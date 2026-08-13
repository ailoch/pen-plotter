import copy, math
from lib.geometry import Document, Path, Segment
from lib.settings import LineType, Settings
from lib.infill import _SCALE, _appendLine, _appendLoop, _coverageBand, _difference, _drawArcTolerance, _drawResidue, _fillRegion, _fromClipperPath, _joinType, _offsetPolys, _resolveFillRegion, _toClipperPath

try:
    import pyclipper
except ImportError:
    pyclipper = None

# small tolerance on the pass count calculation to prevent rounding errors giving
# certain strokes extra passes
_PASS_COUNT_REL_TOL = 1e-9

def _passCount(strokeWidth: float, spacing: float) -> int:
    if spacing <= 0:
        return 1
    ratio = strokeWidth / spacing
    nearest = round(ratio)
    if nearest >= 1 and math.isclose(ratio, nearest, rel_tol=_PASS_COUNT_REL_TOL):
        return nearest
    return max(1, math.ceil(ratio))

# offsets from the centerline (in mm) for the passes making up one side of a stroke,
# not counting a center pass. numPasses is the total conceptual pass count (center +
# both sides, for a closed/two-sided stroke); s is the pitch between adjacent passes.
# odd numPasses always includes a center pass at delta=0 (handled separately by the
# caller) with rings spaced every s out to the same outer edge; even numPasses has no
# center pass, so its innermost ring sits at s/2 instead of s so the pitch between the
# two innermost rings (one on each side) still comes out to s. either way the outermost
# delta + s/2 lands exactly at strokeWidth/2.
def _passDeltas(numPasses: int, s: float) -> list[float]:
    if numPasses % 2 == 1:
        return [k * s for k in range(1, (numPasses - 1) // 2 + 1)]
    return [(k - 0.5) * s for k in range(1, numPasses // 2 + 1)]

# caps that already extend past the end point, so their passes land on their own planes
# and need no setback; anything else (including an unrecognized value) is a butt cap
_EXTENDING_CAPS = ("round", "square")

# the plane each butt cap of an open subpath sits on, as (endPoint, outwardUnitTangent).
# The tangent comes from the flattened polyline's own end chord, which is what pyclipper
# builds the cap itself from, so plane and cap stay parallel. Empty if the path is too
# degenerate to have a direction.
def _buttEndPlanes(vertices: list[complex]) -> list[tuple[complex, complex]]:
    planes = []
    for endIndex, step in ((len(vertices) - 1, -1), (0, 1)):
        end = vertices[endIndex]
        i = endIndex + step
        while 0 <= i < len(vertices) and abs(vertices[i] - end) < 1e-12:
            i += step
        if not 0 <= i < len(vertices):
            return []
        planes.append((end, (end - vertices[i]) / abs(end - vertices[i])))
    return planes

# how far a cap cutter has to reach to cover everything outward of an end plane
def _clipReach(vertices: list[complex], strokeWidth: float) -> float:
    xs = [v.real for v in vertices]
    ys = [v.imag for v in vertices]
    return (max(xs) - min(xs)) + (max(ys) - min(ys)) + strokeWidth + 1

# what a pass set back by `setback` must avoid: everything past a butt end plane, except
# what the band still covers to that depth.
#
# The plane term is why this isn't a centerline trim by arc length - eroding a shape by d
# moves a straight boundary inward by exactly d no matter what the geometry behind it does,
# so the cap lands right even where the path curves into its end. The band term keeps the
# cut honest: a stroke that doubles back (a hairpin dash, whose two caps can sit closer
# together than the stroke is wide) genuinely continues past one arm's end plane, and there
# the band is thick enough to survive the erosion and protect itself.
def _capCutter(band: list, planes: list[tuple[complex, complex]], setback: float, reach: float, tolerance: float) -> list:
    assert pyclipper is not None
    beyond = []
    for origin, tangent in planes:
        base = origin - tangent * setback
        side = tangent * 1j * reach
        beyond.append(_toClipperPath([base + side, base - side,
                                      base - side + tangent * reach, base + side + tangent * reach]))
    keep = _offsetPolys(band, -max(setback - tolerance, 0.0)) if band else []
    return _difference(beyond, [keep])

# removes `cutter` from each of `paths`, in clipper-int space. `closed` picks whether they
# are cut as regions (a pass's offset contour) or as open polylines (the center pass).
def _setBack(paths: list, cutter: list, closed: bool) -> list:
    assert pyclipper is not None
    if not cutter:
        return paths
    cutX = [x for path in cutter for x, _ in path]
    cutY = [y for path in cutter for _, y in path]

    kept = []
    for path in paths:
        # every contour is cut on its own. Handing the whole set to one boolean would
        # union them instead, and a stroke that doubles back offsets into overlapping
        # loops - merging those drops the shared centerlines each one was inking.
        xs = [x for x, _ in path]
        ys = [y for _, y in path]
        if (max(xs) < min(cutX) or min(xs) > max(cutX)
                or max(ys) < min(cutY) or min(ys) > max(cutY)):
            kept.append(path) # out of the cutter's reach, so leave it exactly as it is
            continue
        pc = pyclipper.Pyclipper()
        pc.AddPath(path, pyclipper.PT_SUBJECT, closed)
        pc.AddPaths(cutter, pyclipper.PT_CLIP, True)
        if closed:
            kept.extend(pc.Execute(pyclipper.CT_DIFFERENCE, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO))
        else:
            # an open subject can only come back through a PolyTree
            tree = pc.Execute2(pyclipper.CT_DIFFERENCE, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)
            kept.extend(pyclipper.OpenPathsFromPolyTree(tree))
    return kept

# a dash shorter than this (mm) is numeric noise from a boundary landing exactly on a
# pattern tick, not something to draw
_DASH_EPS = 1e-9

# the inked spans of a path of length `total`, as [(startDistance, endDistance), ...] in
# arc length. pattern is Style.dashPattern()'s output, so it's even-length with a
# positive sum: even indices are "on", odd are "off".
def _dashSpans(total: float, pattern: list[float], dashoffset: float) -> list[tuple[float, float]]:
    period = sum(pattern)
    into = dashoffset % period # how far into the pattern the path's start sits

    # walk the pattern from a point `into` BEFORE the path start, so the first interval
    # is the one the path actually begins inside of. Intervals are clipped to [0, total]
    # below, so the part hanging off the front simply never gets emitted.
    spans: list[tuple[float, float]] = []
    cursor = -into
    i = 0
    while cursor < total:
        end = cursor + pattern[i]
        if i % 2 == 0: # "on"
            start, stop = max(cursor, 0.0), min(end, total)
            if stop - start > _DASH_EPS:
                # a zero-length "off" entry ("5 0") leaves two on-spans touching end to
                # end - one dash, not two, so merge rather than making the router sort
                # out a pair of subpaths that meet at a point
                if spans and start - spans[-1][1] <= _DASH_EPS:
                    spans[-1] = (spans[-1][0], stop)
                else:
                    spans.append((start, stop))
        cursor = end
        i = (i + 1) % len(pattern)
        # a pattern may contain zero-length entries ("5 0"), which don't advance the
        # cursor - but a full cycle always advances by period > 0, so this terminates
    return spans

# extracts the piece of a tessellated path covering arc length [a, b] as a list of
# segments. `cumulative[i]` is the arc length at the start of segments[i].
def _extractSpan(segments: list[Segment], cumulative: list[float], lengths: list[float], a: float, b: float) -> list[Segment]:
    out: list[Segment] = []
    for i, seg in enumerate(segments):
        segStart, segLen = cumulative[i], lengths[i]
        segEnd = segStart + segLen
        if segEnd <= a or segStart >= b or segLen <= 0:
            continue
        # arc length is linear in t for both Line and circular Arc (the only things a
        # tessellated path holds), so the span maps to t by simple proportion - no
        # numerical arc-length inversion needed anywhere in this walk
        t0 = max(a - segStart, 0.0) / segLen
        t1 = min(b - segStart, segLen) / segLen
        if t1 - t0 > 0:
            out.append(seg.subsegment(t0, t1))
    return out

# splits one subpath into the open subpaths its dash pattern actually inks. Works on
# the tessellated path (Lines + circular Arcs only) rather than the raw geometry:
# arc length is linear in t for both, so the split is exact arithmetic - dashing
# splines directly would need a numerical arc-length inversion per cut.
def _applyDash(path: Path, pattern: list[float], dashoffset: float, tolerance: float) -> list[Path]:
    tess = path.tessellate(tolerance, allowArcs=True)
    segments = tess.segments
    if not segments:
        return []

    lengths = [s.length() for s in segments]
    cumulative: list[float] = []
    running = 0.0
    for length in lengths:
        cumulative.append(running)
        running += length
    total = running
    if total <= _DASH_EPS:
        return []

    spans = _dashSpans(total, pattern, dashoffset)
    if not spans:
        return []

    dashes = [_extractSpan(segments, cumulative, lengths, a, b) for a, b in spans]
    dashes = [d for d in dashes if d]

    # a closed path whose walk is "on" across the start point is one continuous dash in
    # SVG, joined at the seam rather than capped twice - so stitch the last dash onto
    # the front of the first. Guarded on the spans actually touching both ends, which is
    # what "on across the seam" means in arc-length terms.
    if (len(dashes) > 1 and path.isClosed()
            and spans[0][0] <= _DASH_EPS and spans[-1][1] >= total - _DASH_EPS):
        dashes[0] = dashes[-1] + dashes[0]
        dashes.pop()

    return [Path(segments=d, lineType=path.lineType) for d in dashes]

# generates the multi-pass concentric strokes
# for every PathObject with a set stroke color, appending them as new STROKE-tagged
# subpaths to object.geometry. runs in printer space (mm).
# settings.generateStroke=False (or a missing pyclipper install) disables the multi-pass
# expansion and draws a single centerline STROKE pass per subpath instead
def generateStroke(document: Document, settings: Settings):
    spacing = settings.fillSpacing
    tolerance = settings.tessellationTolerance
    warnedMissingPyclipper = False

    for obj in document.objects:
        style = obj.style
        if style.strokeColor is None or style.strokeWidth <= 0:
            continue

        # only the raw, un-stroked/un-filled centerline geometry is a stroke source -
        # this excludes any loops a previous pass already generated
        rawSubpaths = [p for p in obj.geometry if p.lineType == LineType.RAW_GEOMETRY]
        if not rawSubpaths:
            continue

        # split into dashes BEFORE anything else, so every path below (including the
        # no-pyclipper fallback) works on the pieces that actually get inked. These are
        # new Paths and rawSubpaths is a local list, so obj.geometry's RAW_GEOMETRY
        # entries stay whole - generateInfill still sees the undashed outline it needs.
        pattern = style.dashPattern()
        if pattern:
            rawSubpaths = [d for p in rawSubpaths for d in _applyDash(p, pattern, style.dashoffset, tolerance)]
            if not rawSubpaths:
                continue # every dash landed in a gap

        # settings.generateStroke=False opts out of multi-pass expansion entirely,
        # same fallback as a missing pyclipper install - draws the raw centerline
        # directly as a single STROKE pass (the pre-multi-pass behavior)
        if not settings.generateStroke or pyclipper is None:
            if pyclipper is None and not warnedMissingPyclipper:
                print("Warning: pyclipper is not installed (pip install pyclipper); drawing strokes as a single centerline pass.")
                warnedMissingPyclipper = True
            for p in rawSubpaths:
                centerPass = copy.deepcopy(p)
                centerPass.lineType = LineType.STROKE
                obj.geometry.append(centerPass)
            continue

        numPasses = _passCount(style.strokeWidth, spacing)
        s = style.strokeWidth / numPasses
        centerPassNeeded = numPasses % 2 == 1

        deltas = _passDeltas(numPasses, s)
        joinType = _joinType(style.linejoin)

        # A dashed stroke over a fill splits its band at the centerline: the inner half
        # is territory the fill has to cover continuously anyway (SVG paints fill
        # straight across the dash gaps), and only the outer half is where a gap is
        # meant to show bare paper. So draw just the outward half here and let the fill
        # own the rest, and neither is inked twice. generateInfill's matching half of
        # this deal is its dashPattern() check on hasStroke.
        #
        # Only safe when the fill will actually be drawn - with no fill region, spacing
        # <= 0, or fill disabled, nothing else covers the inner half and the stroke has
        # to stay two-sided.
        fillRegion: list = []
        if pattern and spacing > 0 and style.fillColor is not None:
            try:
                fillRegion = _resolveFillRegion(obj, tolerance)
            except pyclipper.ClipperException as e:
                print(f"Warning: pyclipper failed to resolve fill region for object {obj.id!r} ({e}); drawing its dashes two-sided.")
                fillRegion = []
        oneSided = bool(fillRegion)

        # the ink laid down by every pass of this object, and the true stroke band it is
        # meant to fill, both in clipper-int space - diffed after the loop to find what
        # the passes missed. spacing <= 0 has no coverage width to measure against, so
        # there's nothing to detect
        needResidue = settings.generateGapInfill and spacing > 0
        needBands = needResidue or oneSided
        drawn: list = []
        bands: list = []

        for p in rawSubpaths:
            # closed paths omit the duplicate end point, open ones include it - both
            # match pyclipper's expectation for AddPath
            closed = p.isClosed()
            vertices = p.tessellate(tolerance, allowArcs=False).vertices()
            clipperPath = _toClipperPath(vertices)

            # A butt cap lands every pass's turnaround on the stroke's own end plane, so
            # the passes all re-ink that one line and their +/- s/2 of ink overruns the
            # end. Each pass's cap belongs at its own depth into the band instead
            # (strokeWidth/2 - delta), which is where eroding the band would put it.
            endPlanes: list[tuple[complex, complex]] = []
            if deltas and not oneSided and not closed and len(clipperPath) >= 2 and style.linecap not in _EXTENDING_CAPS:
                endPlanes = _buttEndPlanes(vertices)

            centerPass = None
            if centerPassNeeded and not oneSided:
                centerPass = copy.deepcopy(p) # exact geometry - arcs and beziers survive
                centerPass.lineType = LineType.STROKE
            if centerPass is not None and not endPlanes:
                obj.geometry.append(centerPass)
                if len(clipperPath) >= 2:
                    drawn.append(clipperPath)

            if not deltas and not needBands:
                continue # hairline stroke with nothing to check - the center pass is all of it

            if len(clipperPath) < 2:
                continue

            # closed -> ET_CLOSEDLINE; open -> pyclipper's open-line end type per style.linecap
            endType = pyclipper.ET_CLOSEDLINE if closed else {"round": pyclipper.ET_OPENROUND, "square": pyclipper.ET_OPENSQUARE}.get(style.linecap, pyclipper.ET_OPENBUTT) # default / "butt"

            # one offsetter per subpath, Execute'd at each pass delta in turn (and at
            # strokeWidth/2 for the band) - the loaded path is the same every time
            pco = pyclipper.PyclipperOffset()
            pco.ArcTolerance = _drawArcTolerance(tolerance)
            pco.MiterLimit = style.miterlimit
            pco.AddPath(clipperPath, joinType, endType)

            # the stroke band itself: the shape SVG would have painted solid, offset with
            # the same join/cap/miterlimit the passes used so its edges line up with theirs
            band: list = []
            if needBands or endPlanes:
                try:
                    band = pco.Execute(style.strokeWidth / 2 * _SCALE)
                except pyclipper.ClipperException as e:
                    print(f"Warning: pyclipper stroke band offset failed for object {obj.id!r} ({e}); skipping its gap fill.")
            if needBands:
                bands.extend(band)
            reach = _clipReach(vertices, style.strokeWidth) if endPlanes else 0.0

            # the center pass sits at delta 0, so it sets back by the full strokeWidth/2
            if centerPass is not None and endPlanes:
                for piece in _setBack([clipperPath], _capCutter(band, endPlanes, style.strokeWidth / 2, reach, tolerance), False):
                    _appendLine(obj.geometry, _fromClipperPath(piece), LineType.STROKE, tolerance)
                    drawn.append(piece)

            for delta in (() if oneSided else deltas):
                try:
                    # positive delta grows outward from the centerline - a closed
                    # path's ET_CLOSEDLINE offset yields both the outer ring and the
                    # inner hole in one Execute call; an open path's yields the single
                    # contour wrapping both sides plus caps
                    result = pco.Execute(delta * _SCALE)
                except pyclipper.ClipperException as e:
                    print(f"Warning: pyclipper stroke offset failed for object {obj.id!r} at delta {delta:g}mm ({e}); skipping this pass.")
                    continue
                if endPlanes and result:
                    result = _setBack(result, _capCutter(band, endPlanes, style.strokeWidth / 2 - delta, reach, tolerance), True)
                for contour in result:
                    _appendLoop(obj.geometry, contour, LineType.STROKE, tolerance)
                drawn.extend(result)

        # one-sided: tile the outward half of the dash band (band minus the fill's
        # territory) with the same concentric-rings-plus-residue routine the fill uses,
        # so it inherits that coverage guarantee instead of needing its own argument.
        # firstDelta = penWidth/2 (the real pen's half-width, not the more conservative
        # spacing/2 the interior tiling assumes) puts the first ring's actual ink edge on
        # the region boundary, and rings inset from every boundary - including the
        # centerline cut made by the difference - so the two halves abut there with no seam.
        if oneSided:
            if bands:
                try:
                    outward = _difference(bands, [fillRegion])
                except pyclipper.ClipperException as e:
                    print(f"Warning: pyclipper failed to split object {obj.id!r}'s dash band against its fill ({e}); skipping its stroke.")
                    outward = []
                if outward:
                    _fillRegion(obj.geometry, outward, spacing, settings.penWidth / 2, tolerance, joinType, settings.generateGapInfill, str(obj.id), settings.penWidth, LineType.STROKE)

        # the passes tile the band evenly, but at a join sharp enough for the miterlimit
        # to bevel the spike, each pass gets clipped at a different point and they fan
        # apart faster than spacing - leaving wedges the ink never reaches (likewise
        # inside a curve tighter than strokeWidth/2, where the inner offsets collapse).
        # residue = band - ink, drawn as centerline strokes, same as an infill ring's
        elif bands:
            try:
                residue = _difference(bands, [_coverageBand(drawn, spacing / 2)])
                # the band edge is the true stroke edge, so hold the residue's ink
                # inside it the same way the fill holds its ring-0 residue inside the
                # region boundary
                keepIn = _offsetPolys(bands, -settings.penWidth / 2)
            except pyclipper.ClipperException as e:
                print(f"Warning: pyclipper stroke residue detection failed for object {obj.id!r} ({e}); skipping its gap fill.")
                residue, keepIn = [], []
            _drawResidue(obj.geometry, residue, spacing, tolerance, str(obj.id), keepIn)

# removes RAW_GEOMETRY paths from every object's geometry (they've served their
# purpose as a stroke/fill source and would otherwise confuse the router - a path
# that's never drawn shouldn't factor into travel-distance optimization), then drops
# any object left with no geometry at all
def dropRawGeometry(document: Document):
    survivors = []
    for obj in document.objects:
        obj.geometry = [p for p in obj.geometry if p.lineType != LineType.RAW_GEOMETRY]
        if obj.geometry:
            survivors.append(obj)
        elif document.id.get(obj.id) is obj: # guards against Document.add's known id-collision edge case
            del document.id[obj.id]
    document.objects = survivors
