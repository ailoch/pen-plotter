import ast, math, operator, os, re, tempfile
from dataclasses import dataclass
from typing import TextIO

from lib.geometry import Line, Arc, PathObject, Document
from lib.settings import LineType, Settings

# formats a gcode number: fixed then trimmed of trailing zeros (so 256.0 -> "256",
# 12.5 -> "12.5")
def _fmtNum(v: float | str) -> str:
    return f"{v:.5f}".rstrip("0").rstrip(".")

# per-file drawing state that addLine/penMove diff against to elide redundant gcode -
# a fresh instance is built for every createFile() call so a previous (possibly
# failed) attempt can't leak its final position/feature/speed/accel into the next one
@dataclass
class _DrawState:
    pos: dict[str, float]
    lastMoveType: str = ""
    lastSpeed: float = 0
    lastAccel: float = 0
    lastLineType: LineType | None = None # role of the most recent draw move, for shortTravelThresholds' min-of-both-roles check

def _moveRect(state: _DrawState, settings: Settings, bounds: tuple[float, float, float, float], file: TextIO, lineType: LineType | None = None):
    edges: tuple[complex, complex, complex, complex] = (bounds[0], bounds[1]*1j, bounds[2], bounds[3]*1j)

    _penMove(state, settings, edges[0]+edges[1], file, True)
    _penMove(state, settings, edges[0]+edges[3], file, False, lineType)
    _penMove(state, settings, edges[2]+edges[3], file, False, lineType)
    _penMove(state, settings, edges[2]+edges[1], file, False, lineType)
    _penMove(state, settings, edges[0]+edges[1], file, False, lineType)

# matches a single {...} template block; the inner text is an arithmetic expression
# evaluated against the replace dict, so {TRAVEL_SPEED/2} and {TRAVEL_HEIGHT + 10}
# work, not just a bare {TRAVEL_SPEED}
_TEMPLATE_BLOCK = re.compile(r"\{([^{}]+)\}")

# binary/unary operators permitted in a {...} block. NOTE the deliberate absence of a
# power operator (ast.Pow): it's the one arithmetic op that turns a short expression
# into unbounded work (2**9999999999 pins CPU/RAM), and the template's use cases only
# need +-*/. everything not listed is rejected below.
_TEMPLATE_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_TEMPLATE_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# ensure v is int/float; raises otherwise
def _requireNumber(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"expected a number, got {type(v).__name__}")
    return v

# evaluates one parsed expression node against the replace namespace, allowing ONLY
# variable names (looked up in replace), numeric literals, and the arithmetic operators
# above.
def _evalTemplateNode(node: ast.AST, replace: dict[str, str | float]):
    if isinstance(node, ast.Expression):
        return _evalTemplateNode(node.body, replace)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f"disallowed literal {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id in replace:
            return replace[node.id]
        raise NameError(f"name '{node.id}' is not defined")
    if isinstance(node, ast.BinOp) and type(node.op) in _TEMPLATE_BINOPS:
        left = _requireNumber(_evalTemplateNode(node.left, replace))
        right = _requireNumber(_evalTemplateNode(node.right, replace))
        return _TEMPLATE_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _TEMPLATE_UNARYOPS:
        return _TEMPLATE_UNARYOPS[type(node.op)](_requireNumber(_evalTemplateNode(node.operand, replace)))
    raise ValueError(f"disallowed expression element {type(node).__name__}")

# using eval() here would allow executing arbitrary python code from an input file
# parsing the ast like this guards against arbitrary code execution
# on failure, original {...} text is left untouched and a warning is printed
def _evalTemplateBlock(expr: str, replace: dict[str, str | float]) -> str:
    try:
        result = _evalTemplateNode(ast.parse(expr, mode="eval"), replace)
    except Exception as e:
        print(f"Warning: could not evaluate gcode template expression '{{{expr}}}' ({e}); leaving it as-is")
        return "{" + expr + "}"
    if isinstance(result, (int, float)):
        return _fmtNum(result)
    return str(result)

# adds the contents of srcFile to the end of destFile, substituting {...} expression
# blocks (see _evalTemplateBlock)
def _fileAppend(srcFile: TextIO, destFile: TextIO, replace: dict[str, str | float] = {}):
    for line in srcFile:
        if "{" in line: # this saves time because the regex sub below is slower and most lines don't need it
            line = _TEMPLATE_BLOCK.sub(lambda m: _evalTemplateBlock(m.group(1), replace), line)
        destFile.write(line)

# the next feature name in visualization.style == "segment"'s cycle, given the
# previous one - shared by _addLine's real cycling and _addPath's lookahead (see
# _skipRepeatedClosingColor)
def _nextSegmentType(lastMoveType: str, segmentTypes: tuple[str, ...]) -> str:
    if lastMoveType in segmentTypes:
        idx = (segmentTypes.index(lastMoveType) + 1) % len(segmentTypes)
        return segmentTypes[idx]
    return segmentTypes[0]

# if the closed path's last segment would naturally cycle back to firstColor (the
# color its first segment got), mutate state.lastMoveType as if that repeated color
# had already been drawn - no gcode is written for this synthetic tick, it only
# shifts where the real draw call (right after this) starts its own cycle from, so
# the last segment lands one color further out instead of repeating the first
def _skipRepeatedClosingColor(state: _DrawState, settings: Settings, firstColor: str):
    predicted = _nextSegmentType(state.lastMoveType, settings.segmentTypes)
    if predicted == firstColor:
        state.lastMoveType = predicted

# adds a gcode line to the file with the specified arguments
# param "accel" sets printer accel using m204 in seperate instruction
def _addLine(state: _DrawState, settings: Settings, args: dict[str, str | float | None], file: TextIO, lineType: LineType | None = None):
    if lineType:
        args["F"] = settings.speeds.get(lineType)
        args["accel"] = settings.accels.get(lineType)
        args["type"] = settings.lineTypes.get(lineType)

    line = ""
    lineIsValid = False # lines must contain x, y, or z arg (g2/3 are exempt)
    for param, val in args.items():
        if not val:
            continue
        # check if param is not already set to current value
        if param != "type":
            val = float(val)
        match param:
            case "accel":
                if val != float(state.lastAccel):
                    file.write(f"M204 S{_fmtNum(val)}\n")
                    state.lastAccel = val # type: ignore
                continue
            case "type":
                if settings.styleChangeMessage == "":
                    continue
                feature = ""
                match settings.style:
                    case "role":
                        feature = val
                    case "instruction":
                        if 1 <= int(args["G"]) <= 3: # type: ignore
                            feature = settings.instructionTypes[int(args["G"]) - 1] # type: ignore
                        else:
                            feature = settings.instructionTypes[3]
                    case "segment":
                        feature = _nextSegmentType(state.lastMoveType, settings.segmentTypes)
                if feature != state.lastMoveType and "E" in args:
                    file.write(settings.styleChangeMessage % feature + "\n")
                    state.lastMoveType = feature # type: ignore
                continue
            case "F":
                if val == state.lastSpeed:
                    continue
                state.lastSpeed = val # type: ignore
            case "G":
                if val == 2 or val == 3:
                    lineIsValid = True
            case "X" | "Y" | "Z":
                if val == state.pos[param]:
                    continue
                state.pos[param] = val # type: ignore
                lineIsValid = True

        line += f"{param}{_fmtNum(val)} "
    if lineIsValid:
        file.write(line.strip() + "\n")

# emits a Z move (if needed) to the draw height for lineType - shared by draw-move
# penMoves and by Arc draws, which (unlike Line draws) have no X/Y/Z move of their
# own to piggyback a height change on since G2/G3 only carries the endpoint
def _setDrawHeight(state: _DrawState, settings: Settings, file: TextIO, lineType: LineType | None = None, raised: bool = False):
    newHeight = settings.heights[lineType or LineType.STROKE]
    if raised:
        newHeight += .001
    if state.pos["Z"] != newHeight:
        _addLine(state, settings, {"G": "1", "Z": newHeight}, file)

# moves pen to the specified location
def _penMove(state: _DrawState, settings: Settings, pos: complex, file: TextIO, travel: bool = False, lineType: LineType | None = None, raised: bool = False):
    distSquared = (pos.real - state.pos["X"]) ** 2 + (pos.imag - state.pos["Y"]) ** 2
    if distSquared >= .000001: # moves shorter than .001 mm are probably caused by rounding errors
        if travel:
            # a travel move between two different draw roles must satisfy both roles'
            # thresholds - use whichever is smaller
            threshold = settings.shortTravelThresholds[lineType or LineType.STROKE]
            if state.lastLineType is not None:
                threshold = min(threshold, settings.shortTravelThresholds[state.lastLineType])
            if distSquared >= threshold ** 2: # long travel
                _addLine(state, settings, {"G": "1", "Z": settings.heights[LineType.TRAVEL]}, file, LineType.TRAVEL)
                _addLine(state, settings, {"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file)
                _setDrawHeight(state, settings, file, lineType, raised)
            else: # short travel
                _addLine(state, settings, {"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file, lineType or LineType.STROKE)
        else: # draw moves
            _setDrawHeight(state, settings, file, lineType, raised)
            _addLine(state, settings, {"G": "1", "X": str(pos.real), "Y": str(pos.imag), "E": math.hypot(pos.real-state.pos["X"], pos.imag-state.pos["Y"]) * settings.eAxisMultiplier}, file, lineType or LineType.STROKE)
            state.lastLineType = lineType or LineType.STROKE

# emits one already-classified Line or Arc as a draw move under lineType - the body of
# the old per-segment loop in _addPath, factored out so cropped/marked pieces from
# _splitAtBounds can be emitted the same way as an untouched segment
def _emitSegment(state: _DrawState, settings: Settings, segment: Line | Arc, file: TextIO, lineType: LineType | None, raised: bool):
    if isinstance(segment, Line):
        _penMove(state, settings, segment.start, file, True, lineType, raised=raised)
        _penMove(state, settings, segment.end, file, lineType=lineType, raised=raised)
    elif isinstance(segment, Arc):
        _penMove(state, settings, segment.point(0), file, True, lineType, raised=raised)
        _setDrawHeight(state, settings, file, lineType, raised)
        centerOffset = segment.center - segment.point(0)
        end = segment.point(1)
        params = {"G": "2", "X": end.real, "Y": end.imag, "I": centerOffset.real, "J": centerOffset.imag, "E": segment.length() * settings.eAxisMultiplier}
        if segment.sweep < 0:
            params["G"] = "3"
        _addLine(state, settings, params, file, lineType)
        state.lastLineType = lineType or LineType.STROKE
    else:
        print(f"Unknown path type {type(segment)}")

# the canvas rect in nozzle/gcode space, as (xmin, ymin, xmax, ymax) - segment
# coordinates (post parseSvg transform) are always nozzle space (pen space minus
# penOffset) regardless of visualization.showPenPos, which only affects how the slicer
# LABELS positions in preview, not the real motion
def _canvasBoundsNozzle(settings: Settings) -> tuple[float, float, float, float]:
    minPt = settings.canvasOffset - settings.penOffset
    maxPt = minPt + settings.canvasSize
    return (minPt.real, minPt.imag, maxPt.real, maxPt.imag)

def _inBounds(pt: complex, bounds: tuple[float, float, float, float]) -> bool:
    xmin, ymin, xmax, ymax = bounds
    return xmin <= pt.real <= xmax and ymin <= pt.imag <= ymax

# returns a copy of segment spanning local parameter range [t0, t1] (0 <= t0 <= t1 <= 1)
def _subsegment(segment: Line | Arc, t0: float, t1: float) -> Line | Arc:
    if isinstance(segment, Line):
        return Line(start=segment.point(t0), end=segment.point(t1))
    return Arc(center=segment.center, u=segment.u, v=segment.v, t0=segment.t0 + t0 * segment.sweep, sweep=(t1 - t0) * segment.sweep)

_BOUNDS_T_EPS = 1e-9 # crossings closer than this (in the segment's own [0,1] space) to
# each other or to an endpoint are collapsed - a corner hit registers on both adjacent
# edges and a tangency double-roots, neither of which should yield a zero-length piece

# splits segment into consecutive runs that are each fully inside or fully outside
# `bounds`, returned as [(subsegment, isInBounds), ...] in original segment order.
# Adjacent same-side runs (tangent touches split the curve without changing sides)
# are merged, so the result strictly alternates in/out
def _splitAtBounds(segment: Line | Arc, bounds: tuple[float, float, float, float]) -> list[tuple[Line | Arc, bool]]:
    sxmin, symin, sxmax, symax = segment.bounds()
    bxmin, bymin, bxmax, bymax = bounds
    if sxmin >= bxmin and symin >= bymin and sxmax <= bxmax and symax <= bymax:
        return [(segment, True)]
    if sxmax < bxmin or sxmin > bxmax or symax < bymin or symin > bymax:
        return [(segment, False)]

    corners = (complex(bxmin, bymin), complex(bxmax, bymin), complex(bxmax, bymax), complex(bxmin, bymax))
    ts: list[float] = []
    for i in range(4):
        for pt in segment @ Line(corners[i], corners[(i + 1) % 4]):
            t = segment.tAtPoint(pt)
            if t is not None and _BOUNDS_T_EPS < t < 1 - _BOUNDS_T_EPS: # endpoint grazes split nothing
                ts.append(t)
    ts.sort()

    split = [0.0]
    for t in ts:
        if t - split[-1] > _BOUNDS_T_EPS:
            split.append(t)
    split.append(1.0)

    # classify each piece by its midpoint, merging same-side neighbors
    runs: list[tuple[float, float, bool]] = []
    for i in range(len(split) - 1):
        isIn = _inBounds(segment.point((split[i] + split[i + 1]) / 2), bounds)
        if runs and runs[-1][2] == isIn:
            runs[-1] = (runs[-1][0], split[i + 1], isIn)
        else:
            runs.append((split[i], split[i + 1], isIn))

    if len(runs) == 1: # no side change - keep the original segment object untouched
        return [(segment, runs[0][2])]
    return [(_subsegment(segment, t0, t1), isIn) for t0, t1, isIn in runs]

def _addPath(state: _DrawState, settings: Settings, object: PathObject, file: TextIO, raised: bool = False, outOfBoundsNames: list[str] | None = None):
    bounds = _canvasBoundsNozzle(settings)
    droppedThisObject = False
    for path in object.geometry:
        # RAW_GEOMETRY is a source for stroke/fill generation, never drawn itself
        if path.lineType == LineType.RAW_GEOMETRY:
            continue
        lineType = path.lineType
        tessellated = path.tessellate(settings.tessellationTolerance)
        segments = tessellated.segments

        # style=="segment" cycles segmentTypes once per drawn segment with no notion
        # of shape closure, so a closed path whose segment count doesn't divide evenly
        # against len(segmentTypes) wraps its last segment back onto the same color as
        # its first - which visually merges them, since they're adjacent. Predict the
        # collision before drawing anything: if it'll happen, _skipRepeatedClosingColor
        # pre-advances state.lastMoveType right before the real last-segment draw call,
        # landing one color further out
        firstSegColor = None
        if settings.style == "segment" and len(segments) > 1 and len(settings.segmentTypes) > 1 and tessellated.isClosed():
            firstSegColor = _nextSegmentType(state.lastMoveType, settings.segmentTypes)

        for i, segment in enumerate(segments):
            if not isinstance(segment, (Line, Arc)):
                print(f"Unknown path type {type(segment)}")
                continue
            if firstSegColor is not None and i == len(segments) - 1:
                _skipRepeatedClosingColor(state, settings, firstSegColor)
            for piece, isIn in _splitAtBounds(segment, bounds):
                if isIn:
                    _emitSegment(state, settings, piece, file, lineType, raised)
                else:
                    if not droppedThisObject and outOfBoundsNames is not None:
                        outOfBoundsNames.append(object.id)
                        droppedThisObject = True
                    if settings.showOutOfBounds:
                        _emitSegment(state, settings, piece, file, LineType.INVALID, raised)
                    # else: crop mode - drop the out-of-bounds piece; the next surviving
                    # piece's leading _penMove(travel=True) naturally bridges the gap
    if settings.showBoundingBoxes:
        for path in object.geometry:
            for segment in path.segments:
                _moveRect(state, settings, segment.bounds(), file, LineType._SEGMENT_BOUNDS)
        _moveRect(state, settings, object.bounds(), file, LineType._PATH_BOUNDS)

_EXCLUDE_EPS = 1e-6

# drops points that don't change the polygon's shape
def _removeRedundantPoints(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for p in points:
        if not pts or abs(p[0] - pts[-1][0]) > _EXCLUDE_EPS or abs(p[1] - pts[-1][1]) > _EXCLUDE_EPS:
            pts.append(p)
    if len(pts) > 1 and abs(pts[0][0] - pts[-1][0]) <= _EXCLUDE_EPS and abs(pts[0][1] - pts[-1][1]) <= _EXCLUDE_EPS:
        pts.pop()

    changed = True
    while changed and len(pts) > 2:
        changed = False
        i = 0
        while i < len(pts) and len(pts) > 2:
            prev = pts[i - 1]
            cur = pts[i]
            nxt = pts[(i + 1) % len(pts)]
            ax, ay = cur[0] - prev[0], cur[1] - prev[1]
            bx, by = nxt[0] - cur[0], nxt[1] - cur[1]
            # collinear (zero cross product) and pointing the same way (positive dot)
            # -> cur is a redundant midpoint of a straight edge
            if abs(ax * by - ay * bx) <= _EXCLUDE_EPS and (ax * bx + ay * by) > _EXCLUDE_EPS:
                pts.pop(i)
                changed = True
            else:
                i += 1
    return pts

# joins two boundary contours into one polygon with a zero-width seam
def _bridgeContours(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    bestI, bestJ, bestDist = 0, 0, math.inf
    for i, pa in enumerate(a):
        for j, pb in enumerate(b):
            dist = (pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2
            if dist < bestDist:
                bestI, bestJ, bestDist = i, j, dist
    return a[:bestI + 1] + b[bestJ:] + b[:bestJ + 1] + a[bestI:]

# traces the simple polygon for (plate minus canvas) when the canvas's gap sides
# (the plate edges it does NOT touch) form one contiguous run of 1-3 sides out of the
# 4 (bottom/right/top/left, cyclic CCW index 0-3) - i.e. a |, L, or C shape. plateCorners
# and canvasCorners are each [c0,c1,c2,c3] CCW, where side i runs from corner i to
# corner (i+1)%4 on both rects, so the corner shared between side i and side i+1 is
# canvasCorners[(i+1)%4] (this holds regardless of which sides are gaps).
def _perimeterWalk(gaps: list[bool], plateCorners: list[tuple[float, float]], canvasCorners: list[tuple[float, float]]) -> list[tuple[float, float]]:
    # the run's start side is the one gap side whose preceding side is not a gap
    start = next(i for i in range(4) if gaps[i] and not gaps[(i - 1) % 4])
    run = []
    i = start
    while gaps[i]:
        run.append(i)
        i = (i + 1) % 4
    last = run[-1]

    # the two canvas corners bracketing the run already sit on the plate boundary,
    # since the sides just outside the run are not gaps (canvas touches plate there)
    startCorner = canvasCorners[start]
    endCorner = canvasCorners[(last + 1) % 4]

    polygon = [startCorner]
    for idx in run: # walk the plate boundary CCW across every gap side in the run
        polygon.append(plateCorners[idx])
        polygon.append(plateCorners[(idx + 1) % 4])
    polygon.append(endCorner)

    j = last # walk canvas corners backwards (CW) from just before endCorner, back to startCorner
    while j != start:
        polygon.append(canvasCorners[j])
        j = (j - 1) % 4
    return polygon

# builds the bed_exclude_area polygon (plate minus canvas) as a formatted point string
def _bedExcludeArea(plateSize: complex, canvasMin: complex, canvasMax: complex) -> str:
    plateW, plateH = plateSize.real, plateSize.imag
    # clamp the canvas into the plate so an out-of-bounds canvas (only warned about,
    # never corrected, at load time) can't produce a self-intersecting polygon
    cx0 = max(0.0, canvasMin.real)
    cy0 = max(0.0, canvasMin.imag)
    cx1 = min(plateW, canvasMax.real)
    cy1 = min(plateH, canvasMax.imag)

    plate = [(0.0, 0.0), (plateW, 0.0), (plateW, plateH), (0.0, plateH)] # c0..c3, counterclockwise

    # canvas doesn't overlap the plate -> the whole plate is excluded
    if cx0 >= cx1 - _EXCLUDE_EPS or cy0 >= cy1 - _EXCLUDE_EPS:
        return ",".join(f"{_fmtNum(x)}x{_fmtNum(y)}" for x, y in _removeRedundantPoints(plate))

    # which plate edges (bottom/right/top/left, cyclic CCW) the canvas does NOT reach
    gapB = cy0 > _EXCLUDE_EPS
    gapR = cx1 < plateW - _EXCLUDE_EPS
    gapT = cy1 < plateH - _EXCLUDE_EPS
    gapL = cx0 > _EXCLUDE_EPS
    gaps = [gapB, gapR, gapT, gapL]

    if not any(gaps): # canvas covers the whole plate -> nothing to exclude
        return ""

    canvas = [(cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1)] # k0..k3, counterclockwise

    if all(gaps): # O: canvas touches no edge -> ring (outer plate + inner canvas hole)
        hole = [canvas[0], canvas[3], canvas[2], canvas[1]] # clockwise, opposite winding to plate
        polygon = _bridgeContours(plate, hole)
    elif gapL and gapR and not gapB and not gapT: # ‖: canvas spans full height
        stripL = [(0.0, 0.0), (cx0, 0.0), (cx0, plateH), (0.0, plateH)]
        stripR = [(cx1, 0.0), (plateW, 0.0), (plateW, plateH), (cx1, plateH)]
        polygon = _bridgeContours(stripL, stripR)
    elif gapB and gapT and not gapL and not gapR: # ‖: canvas spans full width
        stripB = [(0.0, 0.0), (plateW, 0.0), (plateW, cy0), (0.0, cy0)]
        stripT = [(0.0, cy1), (plateW, cy1), (plateW, plateH), (0.0, plateH)]
        polygon = _bridgeContours(stripB, stripT)
    else: # |, L, or C: gap sides form one contiguous run -> single simple polygon
        polygon = _perimeterWalk(gaps, plate, canvas)

    return ",".join(f"{_fmtNum(x)}x{_fmtNum(y)}" for x, y in _removeRedundantPoints(polygon))

# writes geom to fileOut as gcode, according to settings
def createFile(geom: Document, settings: Settings, fileOut: str) -> bool:
    state = _DrawState(pos=dict(settings.startPos)) # copy - state.pos is mutated per-move, startPos must not be
    # write to a temp file in the same directory and only swap it in on success,
    # so a failure partway through won't truncate/corrupt a pre-existing fileOut
    outDir = os.path.dirname(os.path.abspath(fileOut)) or "."
    try:
        fd, tempPath = tempfile.mkstemp(dir=outDir, suffix=".tmp")
    except (FileNotFoundError, PermissionError):
        # a temp-file path inside outDir means nothing to the user, who only typed
        # fileOut - report that instead, whether outDir doesn't exist or isn't writable
        print(f'Could not open file "{fileOut}". The directory may not exist or may not be writable.')
        return False

    try:
        with os.fdopen(fd, "w") as destFile:
            replace: dict[str, float | str] = {
                "TRAVEL_HEIGHT": settings.heights[LineType.TRAVEL],
                "TRAVEL_SPEED": settings.speeds[LineType.TRAVEL],
                "TRAVEL_ACCEL": settings.accels[LineType.TRAVEL],
                "LINE_WIDTH": settings.penWidth,
                "LOAD_DELAY": settings.loadDelay,
                "END_X": settings.endPos.real,
                "END_Y": settings.endPos.imag
            }
            if settings.showPenPos:
                canvasMin = settings.canvasOffset
                replace["EXTRUDER_OFFSET"] = f"{_fmtNum(settings.penOffset.real)}x{_fmtNum(settings.penOffset.imag)}"
            else:
                canvasMin = settings.canvasOffset - settings.penOffset
                replace["EXTRUDER_OFFSET"] = "0x2" # 0x2 is the default offset in bambu studio
            replace["BED_EXCLUDE_AREA"] = _bedExcludeArea(settings.plateSize, canvasMin, canvasMin + settings.canvasSize)

            with open(settings.prefixFile, "r") as srcFile:
                _fileAppend(srcFile, destFile, replace)
            destFile.write("\n")

            objectCount = 0
            outOfBoundsNames: list[str] = []
            for object in geom.objects:
                if settings.objectHeightChange and settings.layerChangeMessage != "":
                    destFile.write(settings.layerChangeMessage + "\n\n")
                _addPath(state, settings, object, destFile, objectCount % 2 == 0 and settings.objectHeightChange, outOfBoundsNames)
                objectCount += 1
            if settings.showBoundingBoxes:
                _moveRect(state, settings, geom.bounds(), destFile, LineType._DOCUMENT_BOUNDS)

            destFile.write("\n")
            with open(settings.suffixFile, "r") as srcFile:
                _fileAppend(srcFile, destFile, replace)
        os.replace(tempPath, fileOut)
        tempPath = None
        if outOfBoundsNames:
            action = "Marked as invalid" if settings.showOutOfBounds else "Cropped"
            print(f"\n{action} lines outside the canvas in: {', '.join(outOfBoundsNames)}")
        return True
    except PermissionError as e:
        # os.replace(tempPath, fileOut) reports its src (tempPath) as e.filename
        # even when the real problem is fileOut being locked - show fileOut
        # instead so the message points at a path the user recognizes
        badFile = fileOut if e.filename == tempPath else e.filename
        print(f'Could not open file "{badFile}". Another program might be editing it.')
    except FileNotFoundError as e:
        # os.replace(tempPath, fileOut) reports its src (tempPath) as e.filename
        # even when the real problem is outDir having vanished mid-write - show
        # fileOut instead so the message points at a path the user recognizes
        badFile = fileOut if e.filename == tempPath else e.filename
        print(f'Could not find file "{badFile}".')
    finally:
        if tempPath and os.path.exists(tempPath):
            os.remove(tempPath)
    return False
