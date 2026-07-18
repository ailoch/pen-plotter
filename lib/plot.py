import math, os, tempfile
from dataclasses import dataclass
from typing import TextIO

from lib.geometry import Line, Arc, PathObject, Document
from lib.settings import State, Settings

# per-file drawing state that addLine/penMove diff against to elide redundant gcode -
# a fresh instance is built for every createFile() call so a previous (possibly
# failed) attempt can't leak its final position/feature/speed/accel into the next one
@dataclass
class _DrawState:
    pos: dict[str, float]
    lastMoveType: str = ""
    lastSpeed: float = 0
    lastAccel: float = 0

def _moveRect(state: _DrawState, settings: Settings, bounds: tuple[float, float, float, float], file: TextIO, lineType: State | None = None):
    edges: tuple[complex, complex, complex, complex] = (bounds[0], bounds[1]*1j, bounds[2], bounds[3]*1j)

    _penMove(state, settings, edges[0]+edges[1], file, True)
    _penMove(state, settings, edges[0]+edges[3], file, False, lineType)
    _penMove(state, settings, edges[2]+edges[3], file, False, lineType)
    _penMove(state, settings, edges[2]+edges[1], file, False, lineType)
    _penMove(state, settings, edges[0]+edges[1], file, False, lineType)

# adds the contents of srcFile to the end of destFile
def _fileAppend(srcFile: TextIO, destFile: TextIO, replace: dict[str, str | float] = {}):
    for line in srcFile:
        if "{" in line: # this saves time because the following check is much slower and most lines don't need it
            for k, v in replace.items():
                line = line.replace("{" + k + "}", str(v))
        destFile.write(line)

# adds a gcode line to the file with the specified arguments
# param "accel" sets printer accel using m204 in seperate instruction
def _addLine(state: _DrawState, settings: Settings, args: dict[str, str | float | None], file: TextIO, lineType: State | None = None):
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
                    file.write(f"M204 S{val}\n")
                    state.lastAccel = val # type: ignore
                continue
            case "type":
                feature = ""
                match settings.style:
                    case "line type":
                        feature = val
                    case "instruction":
                        if 1 <= int(args["G"]) <= 3: # type: ignore
                            feature = settings.instructionTypes[int(args["G"]) - 1] # type: ignore
                        else:
                            feature = settings.instructionTypes[3]
                    case "segment":
                        segmentTypes = settings.segmentTypes
                        if state.lastMoveType in segmentTypes:
                            idx = (segmentTypes.index(state.lastMoveType) + 1) % len(segmentTypes)
                            feature = segmentTypes[idx]
                        else:
                            feature = segmentTypes[0]
                if feature != state.lastMoveType and "E" in args:
                    file.write(f"; FEATURE: {feature}\n")
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

        line += f"{param}{f"{val:.5f}".rstrip("0").rstrip(".")} "
    if lineIsValid:
        file.write(line.strip() + "\n")

# moves pen to the specified location
def _penMove(state: _DrawState, settings: Settings, pos: complex, file: TextIO, travel: bool = False, lineType: State | None = None, raised: bool = False):
    distSquared = (pos.real - state.pos["X"]) ** 2 + (pos.imag - state.pos["Y"]) ** 2
    if distSquared >= .000001: # moves shorter than .001 mm are probably caused by rounding errors
        if travel:
            if distSquared >= settings.shortTravelThreshold ** 2: # long travel
                _addLine(state, settings, {"G": "1", "Z": settings.heights[State.TRAVEL]}, file, State.TRAVEL)
                _addLine(state, settings, {"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file)
                newHeight = settings.heights[State.DRAW]
                if raised:
                    newHeight += .001
                _addLine(state, settings, {"G": "1", "Z": newHeight}, file)
            else: # short travel
                _addLine(state, settings, {"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file, State.DRAW)
        else: # draw moves
            newHeight = settings.heights[lineType or State.DRAW]
            if raised:
                newHeight += .001
            if state.pos["Z"] != newHeight:
                _addLine(state, settings, {"G": "1", "Z": newHeight}, file)
            _addLine(state, settings, {"G": "1", "X": str(pos.real), "Y": str(pos.imag), "E": math.hypot(pos.real-state.pos["X"], pos.imag-state.pos["Y"])}, file, lineType or State.DRAW)

def _addPath(state: _DrawState, settings: Settings, object: PathObject, file: TextIO, raised: bool = False):
    for path in object.geometry:
        tessellated = path.tessellate(settings.tessellationTolerance)
        for segment in tessellated.segments:
            if isinstance(segment, Line):
                _penMove(state, settings, segment.start, file, True, raised=raised)
                _penMove(state, settings, segment.end, file, raised=raised)
            elif isinstance(segment, Arc):
                _penMove(state, settings, segment.point(0), file, True, raised=raised)
                centerOffset = segment.center - segment.point(0)
                end = segment.point(1)
                params = {"G": "2", "X": end.real, "Y": end.imag, "I": centerOffset.real, "J": centerOffset.imag, "E": segment.length()}
                if segment.sweep < 0:
                    params["G"] = "3"
                _addLine(state, settings, params, file, State.DRAW)
            else:
                print(f"Unknown path type {type(segment)}")
    if settings.showBoundingBoxes:
        for path in object.geometry:
            for segment in path.segments:
                _moveRect(state, settings, segment.bounds(), file, State._SEGMENT_BOUNDS)
        _moveRect(state, settings, object.bounds(), file, State._PATH_BOUNDS)

# writes geom to fileOut as gcode, according to settings
def createFile(geom: Document, settings: Settings, fileOut: str) -> bool:
    state = _DrawState(pos=dict(settings.startPos)) # copy - state.pos is mutated per-move, startPos must not be
    # write to a temp file in the same directory and only swap it in on success,
    # so a failure partway through won't truncate/corrupt a pre-existing fileOut
    tempPath = None
    try:
        outDir = os.path.dirname(os.path.abspath(fileOut)) or "."
        fd, tempPath = tempfile.mkstemp(dir=outDir, suffix=".tmp")
        with os.fdopen(fd, "w") as destFile:
            replace: dict[str, float | str] = {
                "TRAVEL_HEIGHT": settings.heights[State.TRAVEL],
                "TRAVEL_SPEED": settings.speeds[State.TRAVEL],
                "TRAVEL_ACCEL": settings.accels[State.TRAVEL],
                "LINE_WIDTH": settings.penWidth,
                "LOAD_DELAY": settings.loadDelay,
                "END_X": settings.endPos.real,
                "END_Y": settings.endPos.imag
            }
            plateMaxX = settings.plateSize.real
            plateMaxY = settings.plateSize.imag
            canvasMaxX = settings.drawableArea.real
            canvasMaxY = settings.drawableArea.imag
            if settings.showPenPos:
                replace["BED_EXCLUDE_AREA"] = f"0x0,{plateMaxX}x0,{plateMaxX}x{plateMaxY},{canvasMaxX}x{plateMaxY},{canvasMaxX}x{plateMaxY-canvasMaxY},0x{plateMaxY-canvasMaxY}"
                replace["EXTRUDER_OFFSET"] = f"{settings.penOffset.real}x{settings.penOffset.imag}"
            else:
                replace["BED_EXCLUDE_AREA"] = f"0x0,{plateMaxX}x0,{plateMaxX}x{plateMaxY-canvasMaxY},{plateMaxX-canvasMaxX}x{plateMaxY-canvasMaxY},{plateMaxX-canvasMaxX}x{plateMaxY},0x{plateMaxY}"
                replace["EXTRUDER_OFFSET"] = "0x2" # 0x2 is the default offset

            with open(settings.prefixFile, "r") as srcFile:
                _fileAppend(srcFile, destFile, replace)
            destFile.write("\n")

            objectCount = 0
            for object in geom.objects:
                _addPath(state, settings, object, destFile, objectCount % 2 == 0 and settings.objectHeightChange)
                objectCount += 1
            if settings.showBoundingBoxes:
                _moveRect(state, settings, geom.bounds(), destFile, State._DOCUMENT_BOUNDS)

            destFile.write("\n")
            with open(settings.suffixFile, "r") as srcFile:
                _fileAppend(srcFile, destFile, replace)
        os.replace(tempPath, fileOut)
        tempPath = None
        return True
    except PermissionError as e:
        # os.replace(tempPath, fileOut) reports its src (tempPath) as e.filename
        # even when the real problem is fileOut being locked - show fileOut
        # instead so the message points at a path the user recognizes
        badFile = fileOut if e.filename == tempPath else e.filename
        print(f'Could not open file "{badFile}". Another program might be editing it.')
    except FileNotFoundError as e:
        print(f'Could not find file "{e.filename}".')
    finally:
        if tempPath and os.path.exists(tempPath):
            os.remove(tempPath)
    return False
