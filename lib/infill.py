import math

from lib.geometry import Path, Document, Line, Arc, Segment

try:
    import pyclipper
except ImportError:
    pyclipper = None

_SCALE = 1e5 # pyclipper needs integer coordinates; this gives ~10nm precision at mm scale

def _toClipperPath(points: list[complex]) -> list[tuple[int, int]]:
    return [(round(p.real * _SCALE), round(p.imag * _SCALE)) for p in points]

def _fromClipperPath(path) -> list[complex]:
    return [complex(x / _SCALE, y / _SCALE) for x, y in path]

# recursively fits a polyline (a discrete point list, e.g. from pyclipper) to a
# single Line or circular Arc within tolerance (mm), falling back to bisecting the
# range when neither fits. mirrors Segment._fitToTolerance, but works on a
# discrete point list rather than a continuous point(t) function - used to turn
# pyclipper's straight-line offset output back into clean arcs, so e.g. a
# circular fill's infill loops come back out as circles (a couple of G2/G3s)
# instead of many-sided polygons (many G1s)
def _fitPointsToTolerance(points: list[complex], tolerance: float, maxDepth: int, depth: int = 0) -> list[Segment]:
    p0, p1 = points[0], points[-1]
    interior = points[1:-1]

    if depth >= maxDepth or len(points) <= 2:
        return [Line(p0, p1)]

    # --- try a Line ---
    chord = p1 - p0
    chordLen = abs(chord)
    if chordLen < 1e-9:
        maxDev = max((abs(p - p0) for p in interior), default=0.0)
    else:
        chordDir = chord / chordLen
        maxDev = max((abs(((p - p0) * chordDir.conjugate()).imag) for p in interior), default=0.0)

    if maxDev <= tolerance:
        return [Line(p0, p1)]

    # --- try a circular Arc via 3-point circumcircle ---
    midIdx = len(points) // 2
    pm = points[midIdx]

    arc = Arc.fromThreePoints(p0, pm, p1)
    if arc is not None:
        maxRadialDev = max((abs(abs(p - arc.center) - abs(arc.u)) for p in interior), default=0.0)
        if maxRadialDev <= tolerance:
            return [arc]

    # --- neither fit: split at the midpoint and recurse on each half ---
    left = _fitPointsToTolerance(points[:midIdx + 1], tolerance, maxDepth, depth + 1)
    right = _fitPointsToTolerance(points[midIdx:], tolerance, maxDepth, depth + 1)
    return left + right

# angle above which a polyline vertex is treated as a genuine corner that arc
# fitting must never span across. any 3 non-collinear points define SOME circle,
# so a large-radius one can closely hug two straight edges meeting at a gentle
# angle - unlike Segment._fitToTolerance, which only ever fits within a single
# already-smooth original segment (never across a boundary between two
# different segments), _fitPointsToTolerance has no such boundary information
# from a flat point list alone, so real corners must be found first. comfortably
# above the few-degrees-per-step turning angle of a finely-tessellated curve
# (see Arc.toPoints) and comfortably below typical polygon corner angles
_CORNER_ANGLE_THRESHOLD = math.radians(15)

# returns indices of points where the polyline turns sharply (treating the list
# as a closed loop, so index 0's corner-ness is checked too)
def _findCornerIndices(points: list[complex], angleThreshold: float) -> list[int]:
    n = len(points)
    corners = []
    for i in range(n):
        prevDir = points[i] - points[i - 1] # Python's negative indexing wraps naturally
        nextDir = points[(i + 1) % n] - points[i]
        if abs(prevDir) < 1e-9 or abs(nextDir) < 1e-9:
            continue
        cosAngle = (prevDir.real*nextDir.real + prevDir.imag*nextDir.imag) / (abs(prevDir) * abs(nextDir))
        cosAngle = max(-1.0, min(1.0, cosAngle))
        if math.acos(cosAngle) > angleThreshold:
            corners.append(i)
    return corners

# builds a closed Path from a polygon's vertices (as returned by pyclipper), fit
# to lines/arcs within tolerance. splits at detected corners first (see
# _CORNER_ANGLE_THRESHOLD) and fits each smooth run between them independently,
# so a fit never spans across a genuine corner. a loop with no detected corners
# (e.g. a circle) is fit as a whole, closing back to the start - exactly like a
# continuous curve's full [0,1] range would be
def _loopToPath(points: list[complex], tolerance: float, maxDepth: int) -> Path:
    corners = _findCornerIndices(points, _CORNER_ANGLE_THRESHOLD)
    if not corners:
        return Path(_fitPointsToTolerance(points + [points[0]], tolerance, maxDepth))

    segments: list[Segment] = []
    for i in range(len(corners)):
        start = corners[i]
        end = corners[(i + 1) % len(corners)]
        run = points[start:end + 1] if end > start else points[start:] + points[:end + 1]
        segments.extend(_fitPointsToTolerance(run, tolerance, maxDepth))
    return Path(segments)

# generates concentric infill loops for every PathObject with a set fill color,
# appending them as new subpaths to object.geometry (the original perimeter
# subpaths are left untouched, and drawn first - see Plotter.addPath). runs in
# printer space (mm), so must be called after parseSvg's transforms are applied.
# spacing <= 0 disables infill entirely
def generateInfill(document: Document, spacing: float, tolerance: float, maxDepth: int):
    if spacing <= 0:
        return
    if pyclipper is None:
        print("Warning: pyclipper is not installed (pip install pyclipper); skipping infill generation")
        return

    for obj in document.objects:
        if obj.style.fillColor is None:
            continue

        fillableSubpaths = [p for p in obj.geometry if p.isFillable()]
        if not fillableSubpaths:
            continue

        # tessellate with allowArcs=False so every subpath is flattened to Lines
        # only (all pyclipper understands) - Path.vertices() then gives the
        # polygon points directly, correctly including the true final point for
        # open paths (relying on SVG's implicit fill closure back to the start)
        clipperPaths = [_toClipperPath(p.tessellate(tolerance, maxDepth, allowArcs=False).vertices()) for p in fillableSubpaths]
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

        # offset repeatedly from the ORIGINAL resolved region (not chained from the
        # previous loop) to avoid compounding discretization drift - each Execute
        # call recomputes fresh from the paths added below, just with a larger
        # cumulative delta
        #
        # JT_ROUND: rounds gaps at REFLEX (concave) corners during the inward
        # offset (e.g. the inner point of a V-notch, or a self-intersecting
        # shape's crossing-derived cusps) - _loopToPath then reconstructs those as
        # real Arcs. Preferred look over JT_MITER's sharp corners there; confirmed
        # unrelated to the spurious-arc bug on horse.svg's legs (that was genuine
        # sub-visual curvature in the source path, not a join-type artifact).
        pco = pyclipper.PyclipperOffset()
        pco.AddPaths(region, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)

        loops = []
        step = 1
        maxSteps = 10000 # safety net against a pathological infinite loop
        while step <= maxSteps:
            try:
                offsetResult = pco.Execute(-spacing * step * _SCALE)
            except pyclipper.ClipperException as e:
                print(f"Warning: pyclipper offset failed for object {obj.id!r} at step {step} ({e}); stopping infill for it")
                break
            if not offsetResult:
                break
            loops.extend(offsetResult)
            step += 1

        for loopPts in loops:
            realPts = _fromClipperPath(loopPts)
            if len(realPts) < 3:
                continue
            obj.geometry.append(_loopToPath(realPts, tolerance, maxDepth))
