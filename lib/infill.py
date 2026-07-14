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

# tries to fit the ENTIRE given point range to a single Line or circular Arc
# within tolerance (mm); returns None if neither fits. Line is tried first so a
# genuinely (or near-) straight range is never represented as an unnecessary arc
def _tryFit(points: list[complex], tolerance: float) -> Segment | None:
    p0, p1 = points[0], points[-1]
    if len(points) <= 2:
        return Line(p0, p1)

    interior = points[1:-1]

    # --- try a Line ---
    # distance to the finite chord SEGMENT is convex, so a straight sub-edge
    # between two consecutive points can't bulge past both its endpoints - the
    # given points alone bound it, no midpoints needed
    chord = p1 - p0
    chordLen = abs(chord)
    if chordLen < 1e-9:
        maxDev = max((abs(p - p0) for p in interior), default=0.0)
    else:
        chordConj = (chord / chordLen).conjugate()
        def _distToChordSegment(p: complex) -> float:
            rel = (p - p0) * chordConj
            along = max(0.0, min(chordLen, rel.real))
            return math.hypot(rel.real - along, rel.imag)
        maxDev = max((_distToChordSegment(p) for p in interior), default=0.0)

    if maxDev <= tolerance:
        return Line(p0, p1)

    # --- try a circular Arc via 3-point circumcircle ---
    # radial deviation from a circle is NOT convex, so a long, sparsely-sampled
    # straight sub-edge can bulge several mm from the arc between two given
    # points that both satisfy tolerance - so also check the midpoint of every
    # consecutive pair (pyclipper always connects consecutive points with a
    # literal straight sub-edge, so these midpoints are always valid samples)
    midIdx = len(points) // 2
    pm = points[midIdx]

    arc = Arc.fromThreePoints(p0, pm, p1)
    if arc is not None:
        center, r = arc.center, abs(arc.u)
        midpoints = [(points[i] + points[i+1]) / 2 for i in range(len(points) - 1)]
        maxRadialDev = max((abs(abs(p - center) - r) for p in interior + midpoints), default=0.0)
        if maxRadialDev <= tolerance:
            return arc

    return None

# fits a polyline (a discrete point list, e.g. from pyclipper) to a sequence of
# Lines/circular Arcs within tolerance (mm). mirrors Segment._fitToTolerance, but
# works on a discrete point list rather than a continuous point(t) function
#
# greedily consumes the LARGEST prefix that still fits a single Line/Arc,
def _fitPointsToTolerance(points: list[complex], tolerance: float) -> list[Segment]:
    segments: list[Segment] = []
    i = 0
    n = len(points)
    while i < n - 1:
        remaining = points[i:]
        m = len(remaining)

        lo = 2
        found = _tryFit(remaining[:2], tolerance)
        assert found is not None # a 2-point range always succeeds
        size = 2
        hi = m
        while size < m:
            nextSize = min(size * 2, m)
            fit = _tryFit(remaining[:nextSize], tolerance)
            if fit is None:
                hi = nextSize
                break
            lo, found, size = nextSize, fit, nextSize
        else:
            segments.append(found) # whole remaining range fit
            break

        # binary search between the last known-good size (lo) and first
        # known-bad size (hi) found by the gallop above
        while hi - lo > 1:
            mid = (lo + hi) // 2
            fit = _tryFit(remaining[:mid], tolerance)
            if fit is not None:
                lo, found = mid, fit
            else:
                hi = mid

        segments.append(found)
        i += lo - 1
    return segments

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
def _loopToPath(points: list[complex], tolerance: float) -> Path:
    corners = _findCornerIndices(points, _CORNER_ANGLE_THRESHOLD)
    if not corners:
        return Path(_fitPointsToTolerance(points + [points[0]], tolerance))

    segments: list[Segment] = []
    for i in range(len(corners)):
        start = corners[i]
        end = corners[(i + 1) % len(corners)]
        run = points[start:end + 1] if end > start else points[start:] + points[:end + 1]
        segments.extend(_fitPointsToTolerance(run, tolerance))
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
        pco = pyclipper.PyclipperOffset()
        # how finely JT_ROUND flattens its fillet arcs. pyclipper's default
        # (0.25 scaled units ~ 2.5nm here) is far finer than tolerance needs and
        # floods each loop with points that _loopToPath then has to re-fit;
        # tol/4 keeps fillet deviation negligible while cutting the point count
        # (and infill time) several-fold
        pco.ArcTolerance = tolerance / 4 * _SCALE
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
            obj.geometry.append(_loopToPath(realPts, tolerance))
