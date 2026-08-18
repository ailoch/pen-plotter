import math
from typing import Any, cast
from lib.geometry import Line, Path, Document
from lib.settings import LineType, Settings

try:
    import pyclipper
except ImportError:
    pyclipper = None

_SCALE = 1e5 # pyclipper needs integer coordinates; this gives ~10nm precision at mm scale

# how finely a residue piece's boundary is resampled before its medial axis is taken
# the sample pitch sets how well the Voronoi approximates the true skeleton, and drives
# the cost of the whole gap-fill path. counter-intuitively, sampling too coarsely costs
# *more* downstream: the skeleton comes back jittery, which both loses coverage and defeats
# the arc fitter, so it emits many short segments instead of a few long arcs
_MEDIAL_SAMPLE_DIVISOR = 6

# how far each resampled boundary site is nudged to keep qhull out of its degenerate
# collinear case, in clipper units (1 = 10nm)
_MEDIAL_JITTER = 1.0

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

# maps a Style.linejoin string to the corresponding pyclipper join type - shared by
# the fill inset (so it follows a stroke's inner edge the same way the stroke itself
# is drawn) and lib/stroke.py's own offsetting.
def _joinType(linejoin: str):
    assert pyclipper is not None # only called once pyclipper is known to be installed
    return {"round": pyclipper.JT_ROUND, "bevel": pyclipper.JT_SQUARE}.get(linejoin, pyclipper.JT_MITER) # default / "miter"

# converts a clipper-int loop back to a closed, tessellated Path tagged with lineType,
# and appends it to geometry - shared by the concentric and gap-fill loop passes
def _appendLoop(geometry: list[Path], loopPts, lineType: LineType, tolerance: float):
    realPts = _fromClipperPath(loopPts)
    if len(realPts) < 3:
        return
    loop = Path.fromPoints(realPts, closed=True)
    loop.lineType = lineType
    geometry.append(loop.tessellate(tolerance, fitLines=True))

# converts an OPEN polyline (mm-space complex points, e.g. a residue centerline) to a
# tessellated open Path tagged with lineType, and appends it to geometry. consecutive
# near-coincident points are dropped so fromPoints never builds a zero-length Line.
def _appendLine(geometry: list[Path], points: list[complex], lineType: LineType, tolerance: float):
    pts: list[complex] = []
    for p in points:
        if not pts or abs(p - pts[-1]) > 1e-9:
            pts.append(p)
    if len(pts) < 2:
        return
    line = Path.fromPoints(pts, closed=False)
    line.lineType = lineType
    geometry.append(line.tessellate(tolerance, fitLines=True))

# single offset of closed polygons for DETECTION geometry (residue erosion/opening) -
# never drawn, so JT_SQUARE's cheap few-point corners are fine (at nozzle scale the
# round-vs-square difference is negligible) and keep the point count down. asTree
# returns a PolyTree so caller can read hole nesting.
def _offsetPolys(paths: list, delta: float, asTree: bool = False):
    assert pyclipper is not None
    pco = pyclipper.PyclipperOffset()
    pco.AddPaths(paths, pyclipper.JT_SQUARE, pyclipper.ET_CLOSEDPOLYGON)
    return pco.Execute2(delta * _SCALE) if asTree else pco.Execute(delta * _SCALE)

# offsets closed loops treated as CLOSED LINES (not polygons) by +delta, yielding the
# +/- delta band swept around the loop's curve - i.e. the ink a pen of half-width delta
# lays down tracing that loop. used to measure per-ring coverage. JT_SQUARE (detection
# only) keeps corners cheap.
def _coverageBand(centerlines: list, delta: float):
    assert pyclipper is not None
    if not centerlines:
        return []
    pco = pyclipper.PyclipperOffset()
    pco.AddPaths(centerlines, pyclipper.JT_SQUARE, pyclipper.ET_CLOSEDLINE)
    return pco.Execute(delta * _SCALE)

# polygon region boolean: subject minus every (non-empty) clip, nonzero fill. all args
# in clipper-int space.
def _difference(subject: list, clips: list) -> list:
    assert pyclipper is not None
    pc = pyclipper.Pyclipper()
    pc.AddPaths(subject, pyclipper.PT_SUBJECT, True)
    for clip in clips:
        if clip:
            pc.AddPaths(clip, pyclipper.PT_CLIP, True)
    return pc.Execute(pyclipper.CT_DIFFERENCE, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)

# polygon region boolean: subject clipped to clip, nonzero fill. all args in clipper-int
# space.
def _intersect(subject: list, clip: list) -> list:
    assert pyclipper is not None
    pc = pyclipper.Pyclipper()
    pc.AddPaths(subject, pyclipper.PT_SUBJECT, True)
    pc.AddPaths(clip, pyclipper.PT_CLIP, True)
    return pc.Execute(pyclipper.CT_INTERSECTION, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)

# generates a family of concentric inward-offset loops from polygons (clipper-int
# space), returning them in clipper-int space. offsets are taken repeatedly from the
# ORIGINAL polygons (not chained from the previous loop) so discretization drift can't
# compound - each Execute recomputes fresh from the added paths, just with a larger
# cumulative delta of firstDelta + spacing*k (k = 0, 1, 2, ...) until Execute comes
# back empty (the interior is exhausted). spacing/firstDelta are in mm. only used by
# the wide-gap fallback (_fillGap) now that the main fill tiles rings itself.
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
            print(f"Warning: pyclipper offset failed for object {objId!r} at step {step} ({e}); stopping infill for it.")
            break
        if not result:
            break
        loops.extend(result)
        step += 1
    return loops

# walks a pyclipper PolyTree, yielding (outerContour, [holeContours]) for every solid
# (non-hole) node - grouping each filled region with the holes cut directly into it so
# an annular residue (e.g. the sliver ringing an oval's center) is handled as a proper
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

# even-odd (crossing-number) point-in-polygon test for a whole batch of points at once.
# pts is an (N, 2) float array and poly an (M, 2) float array of one closed contour's
# vertices (the closing edge is implied); returns an (N,) bool array, True for inside.
# testing points one at a time through pyclipper.PointInPolygon costs O(M) per call from
# Python and dominated gap fill on pieces with large contours; this resolves the whole
# batch in a few numpy passes. points are processed in chunks so the (chunk, M)
# intermediates stay bounded when a contour is big.
def _pointsInPolygon(pts, poly):
    import numpy as np
    if len(poly) < 3 or len(pts) == 0:
        return np.zeros(len(pts), dtype=bool)
    px, py = poly[:, 0], poly[:, 1]
    qx, qy = np.roll(px, -1), np.roll(py, -1)
    dx, dy = qx - px, qy - py
    out = np.zeros(len(pts), dtype=bool)
    chunk = max(1, 4_000_000 // len(poly))
    for lo in range(0, len(pts), chunk):
        x = pts[lo:lo+chunk, 0][:, None]
        y = pts[lo:lo+chunk, 1][:, None]
        straddles = (py > y) != (qy > y) # edges crossing this point's horizontal ray
        # x where each edge crosses that ray. the divide is only meaningful where the
        # edge straddles (dy != 0 there); elsewhere it yields inf/nan, which the
        # straddles mask discards - and a nan comparison is False anyway
        with np.errstate(divide="ignore", invalid="ignore"):
            xCross = px + dx * (y - py) / dy
        out[lo:lo+chunk] = np.logical_xor.reduce(straddles & (x < xCross), axis=1)
    return out

# approximates the medial axis (skeleton) of one residue piece (outer contour + holes,
# clipper-int space) as a set of open polylines (mm-space complex points). a piece that
# is at most `spacing` wide everywhere is fully inked by a single pen pass tracing its
# skeleton (+/- spacing/2 covers +/- half-width), so this turns the leftover slivers and
# wedges the ring tiling drops into cheap centerline strokes instead of double-passed
# loops. returns None (caller falls back to loop fill) if scipy/numpy is unavailable or
# the Voronoi construction degenerates.
def _medialAxisLines(outer: list, holes: list, spacing: float, tolerance: float) -> list[list[complex]] | None:
    assert pyclipper is not None
    try:
        import numpy as np
        from scipy.spatial import Voronoi
    except ImportError:
        return None

    contours = [outer] + holes
    # densely resample the boundary: the medial axis is only as accurate as the site
    # spacing, so cap each edge at spacing/_MEDIAL_SAMPLE_DIVISOR (in clipper units)
    maxEdge = max(spacing / _MEDIAL_SAMPLE_DIVISOR * _SCALE, 1.0)
    samples: list[tuple[float, float]] = []
    for contour in contours:
        n = len(contour)
        for i in range(n):
            x0, y0 = contour[i]
            x1, y1 = contour[(i + 1) % n]
            dx, dy = x1 - x0, y1 - y0
            steps = max(1, int(math.ceil(math.hypot(dx, dy) / maxEdge)))
            for j in range(steps):
                t = j / steps
                samples.append((x0 + dx * t, y0 + dy * t))
    if len(samples) < 4:
        return None

    # resampling a straight edge lays down a long run of EXACTLY collinear sites, which
    # sends qhull's triangulation quadratic - a 180mm sliver's 15k samples take ~10s
    # against ~0.1s for the same count in general position. Nudging each site by a
    # clipper unit breaks the collinearity.
    try:
        sites = np.array(samples, dtype=float)
        sites += np.random.default_rng(0).uniform(-_MEDIAL_JITTER, _MEDIAL_JITTER, sites.shape)
        vor = Voronoi(sites)
    except Exception:
        return None
    verts = vor.vertices

    # a Voronoi vertex is on the medial axis only if it lies strictly inside the piece
    # (inside the outer contour and outside every hole) - resolved for every vertex in one
    # vectorised point-in-polygon pass. Voronoi vertices seeded by near-boundary sites can
    # sit arbitrarily far out (near-infinite finite coords), so bbox-gate first: it skips
    # the obvious outsiders and keeps those huge values out of the arithmetic.
    outerArr = np.asarray(outer, dtype=float)
    minX, minY = outerArr.min(axis=0)
    maxX, maxY = outerArr.max(axis=0)
    inBox = ((verts[:, 0] >= minX) & (verts[:, 0] <= maxX)
             & (verts[:, 1] >= minY) & (verts[:, 1] <= maxY))
    inside = np.zeros(len(verts), dtype=bool)
    if inBox.any():
        candidates = verts[inBox]
        ok = _pointsInPolygon(candidates, outerArr)
        for h in holes:
            if not ok.any():
                break
            ok &= ~_pointsInPolygon(candidates, np.asarray(h, dtype=float))
        inside[inBox] = ok

    # build the skeleton graph from the interior Voronoi ridges
    adj: dict[int, set[int]] = {}
    for a, b in vor.ridge_vertices:
        if a < 0 or b < 0 or not (inside[a] and inside[b]):
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    if not adj:
        return None

    def edgeKey(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)
    def edgeLen(a: int, b: int) -> float: # mm
        return math.hypot(verts[b][0] - verts[a][0], verts[b][1] - verts[a][1]) / _SCALE

    # prune short leaf spurs. a Voronoi skeleton of a densely-resampled boundary sheds a
    # lot of tiny ridges around every corner and junction; drawn literally, each becomes
    # its own routed subpath and pen lift for a stroke a fraction of a pen width long. a
    # spur runs from a tip (degree 1) to a junction (degree >= 3), and that junction is
    # already inked with radius spacing/2, so a spur shorter than that lays down no ink
    # the neighbouring stroke doesn't already cover. repeat a bounded number of rounds,
    # since removing one spur can expose another a level in.
    spurLimit = spacing / 2
    for _ in range(16):
        removed = False
        for tip in [nd for nd in adj if len(adj[nd]) == 1]:
            if tip not in adj or len(adj[tip]) != 1:
                continue # already consumed by an earlier removal this round
            # walk inward from the tip through degree-2 nodes to the first branch point
            chain = [tip]
            prev, cur = tip, next(iter(adj[tip]))
            length = edgeLen(tip, cur)
            while len(adj[cur]) == 2:
                nxt = next(x for x in adj[cur] if x != prev)
                chain.append(cur)
                length += edgeLen(cur, nxt)
                prev, cur = cur, nxt
            # a walk ending on another tip is a whole standalone component, not a spur -
            # keep it (dropping it would leave that residue piece with no stroke at all)
            if len(adj[cur]) < 3 or length >= spurLimit:
                continue
            chain.append(cur) # the junction itself survives; only the spur unlinks
            for k in range(len(chain) - 1):
                adj[chain[k]].discard(chain[k+1])
                adj[chain[k+1]].discard(chain[k])
            for nd in chain[:-1]:
                if not adj.get(nd):
                    adj.pop(nd, None)
            removed = True
        if not removed:
            break
    adj = {nd: nbrs for nd, nbrs in adj.items() if nbrs}
    if not adj:
        return None

    # decompose into strokes, carrying on THROUGH junctions: ending a stroke at every
    # branch point would split one continuous spine into several subpaths (more pen
    # lifts, and a much larger item count for the router to sort). instead, at each node
    # take whichever unused branch best continues the current heading, so a spine stays
    # one stroke and only genuine side branches start new ones. every edge is still
    # drawn exactly once.
    remaining: set[tuple[int, int]] = {edgeKey(nd, nb) for nd, nbrs in adj.items() for nb in nbrs}

    # unit vector from a to b (zero for coincident vertices), as a complex to match how
    # positions are represented everywhere else - the dot product of two of these is then
    # just (u * v.conjugate()).real
    def direction(a: int, b: int) -> complex:
        d = complex(verts[b][0] - verts[a][0], verts[b][1] - verts[a][1])
        return d / abs(d) if d else 0j

    def liveCount(nd: int) -> int:
        return sum(1 for nb in adj[nd] if edgeKey(nd, nb) in remaining)

    chains: list[list[int]] = []
    while remaining:
        # start at a tip where possible (a stroke should begin at a free end), then a
        # junction, then anywhere left - the last case being a pure cycle, which has no
        # natural start
        startNode = None
        for wanted in (1, 3, 0): # 1 = tip, 3 = junction (degree >= 3), 0 = anything
            for nd in adj:
                if liveCount(nd) and (len(adj[nd]) == wanted if wanted == 1 else len(adj[nd]) >= wanted):
                    startNode = nd
                    break
            if startNode is not None:
                break
        if startNode is None:
            break

        chain = [startNode]
        # a zero heading means "no preferred direction", which scores every option 0 on
        # the first step - max() then returns the first candidate, so the opening edge
        # needs no special case
        cur, heading = startNode, 0j
        while True:
            options = [nb for nb in adj[cur] if edgeKey(cur, nb) in remaining]
            if not options:
                break
            # straightest continuation - largest dot product with the current heading
            nxt = max(options, key=lambda nb: (direction(cur, nb) * heading.conjugate()).real)
            remaining.discard(edgeKey(cur, nxt))
            chain.append(nxt)
            heading = direction(cur, nxt)
            cur = nxt
        if len(chain) > 1:
            chains.append(chain)

    lines: list[list[complex]] = []
    for chain in chains:
        poly = [complex(verts[i][0] / _SCALE, verts[i][1] / _SCALE) for i in chain]
        length = sum(abs(poly[i + 1] - poly[i]) for i in range(len(poly) - 1))
        if length > tolerance: # drop numeric-noise fragments
            lines.append(poly)
    return lines or None

# fills one WIDE residue piece (wider than `spacing` somewhere, so a single centerline
# can't ink it) with concentric loops - the fallback for the rare gap the ring tiling
# leaves that isn't a thin sliver. a fixed inset of spacing/2 would annihilate a piece
# only slightly wider than spacing, so halve the first inset down toward tolerance/2
# until it lands inside; any genuinely wide remainder continues inward at normal spacing.
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
    loops.extend(_concentricLoops(group, spacing, delta + spacing, tolerance, objId))
    return loops

# turns the cleaned residue of one ring's annulus into drawn subpaths appended to
# geometry. each piece (outer + holes) that survives the area threshold becomes either a
# set of centerline strokes (the common case: piece <= spacing wide, so its skeleton
# inks it - cheap open lines) or, if the piece is wider than spacing anywhere, a set of
# concentric fallback loops. everything is tagged GAP_INFILL.
def _drawResidue(geometry: list[Path], residue: list, spacing: float, tolerance: float, objId: str, keepIn: list | None = None):
    assert pyclipper is not None
    if not residue:
        return

    # gap fill is drawn as centerlines, so its ink reaches penWidth/2 to either side of
    # whatever is drawn. keepIn is the region already pulled that far in from the
    # outermost fill/stroke boundary; clipping to it keeps that ink off the bare paper
    # outside the shape. The cost is that a feature narrower than the pen is left
    # uninked rather than inked with overspill.
    if keepIn is not None:
        try:
            residue = _intersect(residue, keepIn) if keepIn else []
        except pyclipper.ClipperException as e:
            print(f"Warning: pyclipper failed to clip gap fill to object {objId!r}'s outline ({e}); skipping it.")
            return
        if not residue:
            return

    # morphological opening (erode by eps then dilate back) drops the hairline numeric
    # slivers that ride an annulus/coverage boundary, without eroding a real gap
    eps = tolerance / 2
    try:
        eroded = _offsetPolys(residue, -eps)
        if not eroded:
            return
        opened = _offsetPolys(eroded, eps, asTree=True)
    except pyclipper.ClipperException as e:
        print(f"Warning: pyclipper residue cleanup failed for object {objId!r} ({e}); skipping some gap fill.")
        return

    # drop specks below a stroke-sized area so we don't pepper the drawing with dots -
    # this also discards the benign sub-penWidth dots between adjacent rings (fillSpacing
    # sits under the real pen width, so the pen already covers them)
    minArea = (spacing / 2 * _SCALE) ** 2

    for outer, holes in _polyTreeGroups(opened):
        if abs(pyclipper.Area(outer)) < minArea:
            continue
        group = [outer] + holes
        # wide piece (survives a spacing/2 erosion) -> loops; thin piece -> skeleton lines
        try:
            wide = bool(_offsetPolys(group, -spacing / 2))
        except pyclipper.ClipperException:
            wide = True
        lines = None if wide else _medialAxisLines(outer, holes, spacing, tolerance)
        if lines is not None:
            for poly in lines:
                _appendLine(geometry, poly, LineType.GAP_INFILL, tolerance)
            continue

        before = len(geometry)
        for loopPts in _fillGap(outer, holes, spacing, tolerance, objId):
            _appendLoop(geometry, loopPts, LineType.GAP_INFILL, tolerance)

        # a piece only just over `spacing` wide insets to a near-degenerate remnant, which
        # survives along only part of the piece's length and can collapse entirely under
        # tessellation - either way those loops can leave most of the piece uninked. So
        # measure against the geometry that actually got appended rather than against what
        # _fillGap returned, and skeletonise the shortfall: it's thinner than the piece
        # was, which is the case the skeleton handles, so this stays one level deep
        drawn = [_toClipperPath(p.tessellate(tolerance, allowArcs=False).vertices()) for p in geometry[before:]]
        try:
            missed = _difference(group, [_coverageBand([p for p in drawn if len(p) >= 2], spacing / 2)])
            missed = _offsetPolys(missed, -eps) if missed else []
            missed = _offsetPolys(missed, eps, asTree=True) if missed else None
        except pyclipper.ClipperException as e:
            print(f"Warning: pyclipper failed to measure the wide-gap remainder for object {objId!r} ({e}); skipping it.")
            continue
        if missed is None:
            continue
        for missedOuter, missedHoles in _polyTreeGroups(missed):
            if abs(pyclipper.Area(missedOuter)) < minArea:
                continue
            for poly in _medialAxisLines(missedOuter, missedHoles, spacing, tolerance) or ():
                _appendLine(geometry, poly, LineType.GAP_INFILL, tolerance)

# fills one resolved fill region (clipper-int space) by tiling concentric rings inward
# and, at every ring, drawing centerline strokes over whatever that ring's ink misses -
# so the fill is coverage-complete by construction rather than relying on a global
# post-pass. results are appended to geometry as `lineType` loops + GAP_INFILL strokes.
#
# insets are measured from the region boundary. ring k's centerline sits at
# d0 + k*spacing (d0 = firstDelta), inking the band [d0+k*s - s/2, d0+k*s + s/2]. the
# annulus that band is meant to tile is [outerInset, innerInset] = [d0+k*s - s/2,
# d0+k*s + s/2]; whatever of that annulus the ring's ink doesn't reach (because the
# feature there was too thin for the inset to leave a closed loop - a dropped outline
# stub, an acute-corner wedge, a fractional-width sliver) is the residue, filled with
# centerline strokes. successive annuli abut exactly (annulusInner_k == annulusOuter_k+1),
# so the region is tiled with no seams and no double bookkeeping. drawResidue is skipped
# when generateGapInfill is off (rings still tile).
def _fillRegion(geometry: list[Path], region: list, spacing: float, firstDelta: float, tolerance: float, joinType, generateGapInfill: bool, objId: str, penWidth: float, lineType: LineType = LineType.INFILL):
    assert pyclipper is not None
    s = spacing
    pco = pyclipper.PyclipperOffset()
    pco.ArcTolerance = _drawArcTolerance(tolerance)
    pco.AddPaths(region, joinType, pyclipper.ET_CLOSEDPOLYGON)

    def inset(depth: float) -> list:
        # depth <= 0 means the region boundary itself (Execute at 0 is a no-op offset)
        return region if depth <= 1e-9 else pco.Execute(-depth * _SCALE)

    maxSteps = 10000 # safety net against a pathological infinite loop
    k = 0
    # ring k's inner edge is ring k+1's outer edge (innerInset_k == outerInset_{k+1}),
    # so carry the previous iteration's annulusInner forward instead of re-offsetting it
    carried: list | None = None
    while k < maxSteps:
        center = firstDelta + k * s
        outerInset = center - s / 2
        innerInset = center + s / 2
        try:
            annulusOuter = carried if carried is not None else inset(outerInset)
            if not annulusOuter:
                break # interior exhausted
            ring = inset(center)
            annulusInner = inset(innerInset) if generateGapInfill else []
            # only ring 0's annulus can reach the region boundary (every later one starts
            # at least s/2 further in than penWidth/2), so that's the only residue whose
            # ink needs holding back from it
            keepIn = inset(penWidth / 2) if generateGapInfill and k == 0 else None
        except pyclipper.ClipperException as e:
            print(f"Warning: pyclipper offset failed for object {objId!r} at ring {k} ({e}); stopping infill for it.")
            break
        # annulusInner is only computed for the gapfill path; without it the next
        # iteration re-offsets its outer edge fresh
        carried = annulusInner if generateGapInfill else None

        for loopPts in ring:
            _appendLoop(geometry, loopPts, lineType, tolerance)

        if generateGapInfill:
            try:
                coverage = _coverageBand(ring, s / 2)
                residue = _difference(annulusOuter, [annulusInner, coverage])
            except pyclipper.ClipperException as e:
                print(f"Warning: pyclipper residue detection failed for object {objId!r} at ring {k} ({e}); skipping its gap fill.")
                residue = []
            _drawResidue(geometry, residue, s, tolerance, objId, keepIn)

        k += 1

# the area an object's fill would flood, resolved under its fill-rule, in clipper-int
# space. Does NOT mutate obj - an unclosed fillable subpath is closed implicitly by
# pyclipper's AddPaths(..., closed=True) rather than by appending a segment
def _resolveFillRegion(obj, tolerance: float) -> list:
    assert pyclipper is not None
    fillable = [p for p in obj.geometry if p.lineType == LineType.RAW_GEOMETRY and p.isFillable()]
    if not fillable:
        return []
    clipperPaths = [_toClipperPath(p.tessellate(tolerance, allowArcs=False).vertices()) for p in fillable]
    clipperPaths = [p for p in clipperPaths if len(p) >= 3]
    if not clipperPaths:
        return []
    fillType = pyclipper.PFT_EVENODD if obj.style.fillRule == "evenodd" else pyclipper.PFT_NONZERO
    pc = pyclipper.Pyclipper()
    pc.AddPaths(clipperPaths, pyclipper.PT_SUBJECT, True)
    return pc.Execute(pyclipper.CT_UNION, fillType, fillType)

# generates infill for every PathObject with a set fill color, appending it as new
# subpaths to object.geometry. runs in printer space (mm), so must be called after
# parseSvg's transforms are applied. settings.fillSpacing <= 0 disables the drawn fill,
# but an open fillable subpath is still closed in place so the drawn outline matches
# the shape SVG would have filled.
def generateInfill(document: Document, settings: Settings):
    spacing = settings.fillSpacing
    tolerance = settings.tessellationTolerance
    if spacing > 0 and pyclipper is None:
        print("Warning: pyclipper is not installed (pip install pyclipper); skipping infill generation.")

    for obj in document.objects:
        if obj.style.fillColor is None:
            continue

        # only the raw, un-stroked/un-filled centerline geometry is a fill source -
        # this excludes any loops already generated by a previous pass (e.g. stroke
        # generation, which runs before infill)
        fillableSubpaths = [p for p in obj.geometry if p.lineType == LineType.RAW_GEOMETRY and p.isFillable()]
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

        try:
            region = _resolveFillRegion(obj, tolerance)
        except pyclipper.ClipperException as e:
            print(f"Warning: pyclipper failed to resolve fill region for object {obj.id!r} ({e}); skipping infill for it.")
            continue
        if not region:
            continue

        # a stroked object's fill must stay clear of the stroke band (the outer strokeWidth/2
        # is inked by the stroke passes) and its first ring should follow the stroke's own
        # linejoin at corners so it hugs the stroke's inner edge rather than the (possibly
        # sharper) raw outline. the strokeWidth/2 gap is folded into the first inset, so the
        # ring tiling still measures everything from the raw region boundary.
        #
        # the first ring's own half of that inset is penWidth/2, not spacing/2: the ring's
        # centerline needs to sit exactly penWidth/2 (the real pen's half-width) back from
        # whatever boundary it's meeting, so the pen's actual ink - not the more conservative
        # spacing/2 the interior tiling assumes - lands its outer edge exactly there instead
        # of stopping short of it (a visible white sliver) or bleeding past it
        #
        # a dashed stroke is deliberately excluded: it only inks part of that outer band,
        # so insetting past it would leave an uninked notch in every dash gap
        hasStroke = obj.style.strokeColor is not None and obj.style.strokeWidth > 0 and obj.style.dashPattern() is None
        firstDelta = (obj.style.strokeWidth / 2 + settings.penWidth / 2) if hasStroke else settings.penWidth / 2
        joinType = _joinType(obj.style.linejoin) if hasStroke else pyclipper.JT_ROUND

        _fillRegion(obj.geometry, region, spacing, firstDelta, tolerance, joinType, settings.generateGapInfill, str(obj.id), settings.penWidth)
