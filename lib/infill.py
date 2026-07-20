from typing import Any, cast
from lib.geometry import Line, Path, Document
from lib.settings import LineType, Settings

try:
    import pyclipper
except ImportError:
    pyclipper = None

_SCALE = 1e5 # pyclipper needs integer coordinates; this gives ~10nm precision at mm scale

# how finely JT_ROUND flattens its fillet arcs on the DRAWN loops. pyclipper's default
# (0.25 scaled units ~ 2.5nm here) is far finer than tolerance needs and floods each
# loop with points that tessellate() then has to re-fit; tol/4 keeps fillet deviation
# negligible while cutting the point count (and infill time) several-fold
def _drawArcTolerance(tolerance: float) -> float:
    return tolerance / 4 * _SCALE

def _toClipperPath(points: list[complex]) -> list[tuple[int, int]]:
    return [(round(p.real * _SCALE), round(p.imag * _SCALE)) for p in points]

def _fromClipperPath(path) -> list[complex]:
    return [complex(x / _SCALE, y / _SCALE) for x, y in path]

# converts a clipper-int loop back to a closed, tessellated Path tagged with lineType,
# and appends it to geometry - shared by the concentric and gap-fill loop passes
def _appendLoop(geometry: list[Path], loopPts, lineType: LineType, tolerance: float):
    realPts = _fromClipperPath(loopPts)
    if len(realPts) < 3:
        return
    loop = Path.fromPoints(realPts, closed=True)
    loop.lineType = lineType
    geometry.append(loop.tessellate(tolerance, fitLines=True))

# single offset of closed polygons for the DETECTION geometry (the gap-fill opening
# pass) - never drawn, so JT_SQUARE's cheap few-point corners are fine (at nozzle scale
# the round-vs-square difference is negligible) and keep the point count down. asTree
# returns a PolyTree so caller can read hole nesting.
def _offsetPolys(paths: list, delta: float, asTree: bool = False):
    assert pyclipper is not None # only called from _gapFill, which already checked
    pco = pyclipper.PyclipperOffset()
    pco.AddPaths(paths, pyclipper.JT_SQUARE, pyclipper.ET_CLOSEDPOLYGON)
    return pco.Execute2(delta * _SCALE) if asTree else pco.Execute(delta * _SCALE)

# generates a family of concentric inward-offset loops from polygons (clipper-int
# space), returning them in clipper-int space. offsets are taken repeatedly from the
# ORIGINAL polygons (not chained from the previous loop) so discretization drift can't
# compound - each Execute recomputes fresh from the added paths, just with a larger
# cumulative delta of firstDelta + spacing*k (k = 0, 1, 2, ...) until Execute comes
# back empty (the interior is exhausted). spacing/firstDelta are in mm.
def _concentricLoops(polygons: list, spacing: float, firstDelta: float, tolerance: float, objId: str) -> list:
    if pyclipper is None:
        return []
    pco = pyclipper.PyclipperOffset()
    pco.ArcTolerance = _drawArcTolerance(tolerance)
    pco.AddPaths(polygons, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)

    loops = []
    step = 0
    maxSteps = 10000 # safety net against a pathological infinite loop
    while step < maxSteps:
        delta = firstDelta + spacing * step
        try:
            result = pco.Execute(-delta * _SCALE)
        except pyclipper.ClipperException as e:
            print(f"Warning: pyclipper offset failed for object {objId!r} at step {step} ({e}); stopping infill for it")
            break
        if not result:
            break
        loops.extend(result)
        step += 1
    return loops

# walks a pyclipper PolyTree, yielding (outerContour, [holeContours]) for every solid
# (non-hole) node - grouping each filled region with the holes cut directly into it so
# an annular gap (e.g. the sliver ringing an oval's center) is filled as a proper
# polygon-with-holes rather than as two unrelated boundaries. islands nested inside a
# hole surface as their own later groups.
def _polyTreeGroups(tree) -> list[tuple[list, list]]:
    groups: list[tuple[list, list]] = []
    def walk(node):
        for child in cast(Any, node).Childs:
            if not child.IsHole:
                holes = [h.Contour for h in child.Childs if h.IsHole]
                groups.append((child.Contour, holes))
            walk(child)
    walk(tree)
    return groups

# fills one gap region (an outer boundary plus its holes, clipper-int space) with
# loops. a fixed inset of spacing/2 would annihilate a gap narrower than spacing, so
# halve the first inset down toward tolerance/2 until it lands inside the region; that
# first loop approximates the gap's centerline (the pen tracing around a thin sliver -
# down one side and back is acceptable), and any genuinely wide gap continues inward at
# normal spacing from there.
def _fillGap(outer: list, holes: list, spacing: float, tolerance: float, objId: str) -> list:
    if pyclipper is None:
        return []
    group = [outer] + holes

    delta = spacing / 2
    minDelta = tolerance / 2
    firstResult = []
    while delta > minDelta:
        pco = pyclipper.PyclipperOffset()
        pco.ArcTolerance = _drawArcTolerance(tolerance)
        pco.AddPaths(group, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        try:
            firstResult = pco.Execute(-delta * _SCALE)
        except pyclipper.ClipperException:
            firstResult = []
        if firstResult:
            break
        delta /= 2

    if not firstResult:
        return []

    loops = list(firstResult)
    # continue inward at normal spacing from the same original group
    loops.extend(_concentricLoops(group, spacing, delta + spacing, tolerance, objId))
    return loops

# finds the region of the fill NOT within spacing/2 of any drawn centerline (the outline
# plus every concentric loop) and returns loops that fill it, in clipper-int space.
# coverage is the Minkowski band of radius spacing/2 around every centerline; the leftover
# difference is where the acute-corner wedges and the fractional-width slivers live. a
# morphological opening plus an area threshold then drop numeric specks (and the benign
# sub-penWidth dots between adjacent loops, which the real pen covers) before drawing.
def _gapFill(region: list, centerlines: list, spacing: float, tolerance: float, objId: str) -> list:
    if pyclipper is None or not region:
        return []

    try:
        pco = pyclipper.PyclipperOffset()
        pco.AddPaths(centerlines, pyclipper.JT_SQUARE, pyclipper.ET_CLOSEDLINE)
        covered = pco.Execute(spacing / 2 * _SCALE)
    except pyclipper.ClipperException as e:
        print(f"Warning: pyclipper failed building gap-fill coverage for object {objId!r} ({e}); skipping gap fill for it")
        return []
    if not covered:
        covered = []

    try:
        pc = pyclipper.Pyclipper()
        pc.AddPaths(region, pyclipper.PT_SUBJECT, True)
        if covered:
            pc.AddPaths(covered, pyclipper.PT_CLIP, True)
        gaps = pc.Execute(pyclipper.CT_DIFFERENCE, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)
    except pyclipper.ClipperException as e:
        print(f"Warning: pyclipper difference failed for object {objId!r} ({e}); skipping gap fill for it")
        return []
    if not gaps:
        return []

    # morphological opening (erode by eps then dilate back) drops hairline numeric slivers
    # that ride the coverage boundary, without eroding real gaps (a sizable fraction of
    # spacing wide)
    eps = tolerance / 2
    try:
        eroded = _offsetPolys(gaps, -eps)
        if not eroded:
            return []
        opened = _offsetPolys(eroded, eps, asTree=True)
    except pyclipper.ClipperException as e:
        print(f"Warning: pyclipper opening failed for object {objId!r} ({e}); skipping gap fill for it")
        return []

    # drop specks below a stroke-sized area so we don't pepper the drawing with dots. this
    # also discards the benign sub-penWidth dots the pen already covers (the periodic
    # remnants between adjacent concentric loops), which sit well under this threshold
    minArea = (spacing / 2 * _SCALE) ** 2

    loops = []
    for outer, holes in _polyTreeGroups(opened):
        if abs(pyclipper.Area(outer)) < minArea:
            continue
        loops.extend(_fillGap(outer, holes, spacing, tolerance, objId))
    return loops

# generates concentric infill loops for every PathObject with a set fill color,
# appending them as new subpaths to object.geometry. runs in printer space (mm),
# so must be called after parseSvg's transforms are applied.
# settings.infillSpacing <= 0 disables the concentric loops but closing of
# fillable subpaths (see below) still happens
def generateInfill(document: Document, settings: Settings):
    spacing = settings.infillSpacing
    tolerance = settings.tessellationTolerance
    if spacing > 0 and pyclipper is None:
        print("Warning: pyclipper is not installed (pip install pyclipper); skipping infill generation")

    for obj in document.objects:
        if obj.style.fillColor is None:
            continue

        fillableSubpaths = [p for p in obj.geometry if p.isFillable()]
        if not fillableSubpaths:
            continue

        # a fillable subpath's outline may not actually return to its start point
        # (e.g. an SVG path missing a trailing "Z") - close it in place so the drawn
        # outline matches the shape that's being filled
        for p in fillableSubpaths:
            if not p.isClosed():
                p.segments.append(Line(p.end(), p.start()))

        if spacing <= 0 or pyclipper is None:
            continue

        # tessellate with allowArcs=False so every subpath is flattened to Lines
        # only (all pyclipper understands) - Path.vertices() then gives the
        # polygon points directly (subpaths are already closed above)
        clipperPaths = [_toClipperPath(p.tessellate(tolerance, allowArcs=False).vertices()) for p in fillableSubpaths]
        clipperPaths = [p for p in clipperPaths if len(p) >= 3]
        if not clipperPaths:
            continue

        fillType = pyclipper.PFT_EVENODD if obj.style.fillRule == "evenodd" else pyclipper.PFT_NONZERO

        try:
            pc = pyclipper.Pyclipper()
            pc.AddPaths(clipperPaths, pyclipper.PT_SUBJECT, True)
            region = pc.Execute(pyclipper.CT_UNION, fillType, fillType)
        except pyclipper.ClipperException as e:
            print(f"Warning: pyclipper failed to resolve fill region for object {obj.id!r} ({e}); skipping infill for it")
            continue
        if not region:
            continue

        # the drawn loops - offset inward from the resolved region at spacing intervals
        loops = _concentricLoops(region, spacing, spacing, tolerance, str(obj.id))
        for loopPts in loops:
            _appendLoop(obj.geometry, loopPts, LineType.INFILL, tolerance)

        # fill whatever those loops (plus the outline) leave uncovered: acute-corner
        # wedges and fractional-width slivers. coverage is measured against every drawn
        # centerline, so gap strokes land only where the pen genuinely misses.
        if settings.generateGapInfill:
            gapLoops = _gapFill(region, clipperPaths + loops, spacing, tolerance, str(obj.id))
            for loopPts in gapLoops:
                _appendLoop(obj.geometry, loopPts, LineType.GAP_INFILL, tolerance)
