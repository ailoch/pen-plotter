"""Unit-level tests for lib/stroke.py.

Whether a stroke *covers* what SVG would paint is measured in test_coverage.py.
What lives here is the behaviour with a specific, checkable right answer: the
pass-offset arithmetic, and the dash walk - where "correct" is an exact arc
length, not an eyeball.

Most dash assertions go through _applyDash directly rather than the whole of
generateStroke, because that's where the answer is a number. The pipeline-level
tests below then check the parts _applyDash can't see: that the split is
actually wired in, and which side of the centerline the ink lands on.
"""
import math

import pytest

pyclipper = pytest.importorskip("pyclipper", reason="stroke generation needs pyclipper")

from lib.geometry import Arc, Document, Path, PathObject, Style, Transform
from lib.infill import _SCALE, _toClipperPath, generateInfill
from lib.settings import LineType
from lib.stroke import _applyDash, _buttEndPlanes, _passCount, _passDeltas, generateStroke

#region helpers

DRAWN = (LineType.STROKE, LineType.GAP_INFILL)

# a 12mm horizontal line and a 10mm square (perimeter 40) - both chosen so the dash
# arithmetic below lands on whole numbers and the expected answers can be written out
LINE_PTS = [0 + 0j, 12 + 0j]
SQUARE_PTS = [0 + 0j, 10 + 0j, 10 + 10j, 0 + 10j]


def _object(points, closed, settings, **style) -> PathObject:
    """A single-subpath PathObject with a black stroke, ready for generateStroke."""
    path = Path.fromPoints(points, closed=closed)
    path.lineType = LineType.RAW_GEOMETRY
    style.setdefault("strokeColor", [0, 0, 0])
    style.setdefault("strokeWidth", 2.0)
    style.setdefault("fillColor", None)
    return PathObject("test", [path], Style(**style), Transform())


def _stroked(obj, settings, withFill=False) -> PathObject:
    document = Document()
    document.add(obj)
    generateStroke(document, settings)
    if withFill:
        generateInfill(document, settings)
    return obj


def _inkPolys(obj, settings, roles=DRAWN):
    """The area the pen actually blackens, as clipper polygons.

    Each drawn subpath swept by +/- fillSpacing/2, honouring whether it's open or
    closed - a dash is an open subpath, and treating it as closed would tack a
    phantom return stroke onto the band.
    """
    pco = pyclipper.PyclipperOffset()
    added = False
    for p in obj.geometry:
        if p.lineType not in roles:
            continue
        pts = _toClipperPath(p.tessellate(settings.tessellationTolerance, allowArcs=False).vertices())
        if len(pts) < 2:
            continue
        endType = pyclipper.ET_CLOSEDLINE if p.isClosed() else pyclipper.ET_OPENROUND
        pco.AddPath(pts, pyclipper.JT_ROUND, endType)
        added = True
    if not added:
        return []
    return pco.Execute(settings.fillSpacing / 2 * _SCALE)


def _area(polys) -> float:
    """Net mm^2. Signed, so clipper's oppositely-wound holes subtract correctly."""
    return sum(pyclipper.Area(c) for c in polys) / (_SCALE ** 2)


def _intersect(a, b):
    if not a or not b:
        return []
    pc = pyclipper.Pyclipper()
    pc.AddPaths(a, pyclipper.PT_SUBJECT, True)
    pc.AddPaths(b, pyclipper.PT_CLIP, True)
    return pc.Execute(pyclipper.CT_INTERSECTION, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)


def _inkLength(paths) -> float:
    return sum(p.length() for p in paths)


#endregion

#region pass deltas


@pytest.mark.parametrize("numPasses", range(1, 12))
def testPassDeltasTileTheStrokeEvenly(numPasses):
    """The passes must sit at an even pitch and reach exactly the stroke's edge.

    This is the whole contract of _passDeltas, and it's pure arithmetic, so it can
    be asserted exactly rather than approximately. Both parities are covered by the
    range: odd counts carry an implicit centre pass at 0 that _passDeltas doesn't
    return, even counts don't.
    """
    width = 3.0
    s = width / numPasses
    deltas = _passDeltas(numPasses, s)

    # rebuild the full set of signed pass positions, mirroring each ring and adding
    # the centre pass the caller draws separately for an odd count
    positions = sorted([-d for d in deltas] + ([0.0] if numPasses % 2 == 1 else []) + deltas)
    assert len(positions) == numPasses, "wrong number of passes for the width"

    gaps = [positions[i + 1] - positions[i] for i in range(len(positions) - 1)]
    assert all(g == pytest.approx(s) for g in gaps), f"uneven pitch: {gaps}"

    # the outermost pass inks out to delta + s/2, which has to land on the true edge
    assert positions[-1] + s / 2 == pytest.approx(width / 2)
    assert positions[0] - s / 2 == pytest.approx(-width / 2)


#endregion

#region pass count


@pytest.mark.parametrize("width,spacing,expected", [
    (0.1, 0.3, 1),   # hairline, well under one spacing
    (0.29, 0.3, 1),  # just under - still one pass
    (0.3, 0.3, 1),   # exactly one spacing: the boundary case, one centreline pass
    (0.31, 0.3, 2),  # genuinely over, so a second pass is real
    (0.6, 0.3, 2),
    (2.0, 0.3, 7),   # 6.67 - a true non-multiple, must round up, not snap to 7 by luck
    (2.1, 0.3, 7),   # 2.1/0.3 is 7.000000000000001 in floating point
    (3.0, 0.3, 10),
])
def testPassCountRoundsUpOnlyWhenTheWidthGenuinelyExceedsTheSpacing(width, spacing, expected):
    assert _passCount(width, spacing) == expected


@pytest.mark.parametrize("width", [0.30000000000000004, 0.29999999999999993])
def testPassCountIgnoresParseNoise(width):
    """An authored stroke-width="0.3" comes back from svgelements' x96/25.4 space a
    few ULPs off, and which side it lands on is a coin flip. Rounding up on the high
    side costs the exact-geometry centre pass: the stroke stops being a deepcopy of
    the source (arcs and beziers preserved) and becomes two tessellated offset rings.
    """
    assert _passCount(width, 0.3) == 1


@pytest.mark.parametrize("spacing", [0, -1])
def testNonPositiveSpacingIsASinglePass(spacing):
    """spacing <= 0 disables fill entirely, so there's no pitch to tile against."""
    assert _passCount(2.0, spacing) == 1


@pytest.mark.parametrize("width,spacing", [(0.3, 0.3), (2.1, 0.3), (0.30000000000000004, 0.3), (5.0, 0.7)])
def testPassCountNeverLetsThePitchExceedTheSpacing(width, spacing):
    """The snap must not buy a lower pass count at the cost of a coarser pitch than
    fillSpacing - that's the guarantee the whole tiling rests on."""
    assert width / _passCount(width, spacing) <= spacing * (1 + 1e-9)


#endregion

#region stroke width


def testWiderStrokeCoversMoreArea(settings):
    """Ink area grows with stroke-width, and tracks width x length."""
    length = abs(LINE_PTS[1] - LINE_PTS[0])
    areas = []
    for width in (0.5, 1.0, 2.0, 4.0):
        obj = _stroked(_object(LINE_PTS, False, settings, strokeWidth=width), settings)
        area = _area(_inkPolys(obj, settings))
        areas.append(area)
        # caps and the +/- spacing/2 sweep add a little, so this is a floor-and-ceiling
        # rather than an equality - but a stroke that ignored its width would miss it
        assert length * width <= area <= length * width + 4 * width, (
            f"width {width}: {area:.3f} mm^2 is not about {length * width:.3f}"
        )
    assert areas == sorted(areas), f"area did not grow with width: {areas}"


#endregion

#region butt cap setback


def _trueFootprint(points, closed, strokeWidth):
    """The exact region SVG would paint: the raw centerline offset by strokeWidth/2,
    butt-capped (no extension past the endpoints) since that's the only cap these
    tests exercise."""
    endType = pyclipper.ET_CLOSEDLINE if closed else pyclipper.ET_OPENBUTT
    pco = pyclipper.PyclipperOffset()
    pco.AddPath(_toClipperPath(points), pyclipper.JT_MITER, endType)
    return pco.Execute(strokeWidth / 2 * _SCALE)


def _inkOutsideFootprint(obj, points, closed, strokeWidth, settings) -> float:
    """mm^2 of STROKE ink lying outside the true footprint, inflated by fillSpacing/2 -
    a centerline sitting exactly on the true edge still sweeps that far past it, which
    is expected overcoverage rather than a cap landing in the wrong place."""
    from lib.infill import _difference, _offsetPolys
    footprint = _trueFootprint(points, closed, strokeWidth)
    allowed = _offsetPolys(footprint, settings.fillSpacing / 2)
    ink = _inkPolys(obj, settings, roles=(LineType.STROKE,))
    outside = _difference(ink, [allowed]) if ink else []
    # opened at tolerance: a boolean on tessellated geometry always leaves hairline
    # seams along a shared boundary
    outside = _offsetPolys(outside, -settings.tessellationTolerance) if outside else []
    return _area(outside)


def testDegenerateSegmentShorterThanItsOwnWidthDoesNotOverink(settings):
    """A stroke whose width exceeds its own length forces every pass's two end-plane
    setbacks to overlap - the innermost passes end up entirely erased. generateStroke
    must survive that without crashing, and whatever passes DO survive must still
    respect the true (butt-capped) footprint rather than spilling past it.
    """
    points = [0 + 0j, 1 + 0j]  # 1mm long
    obj = _stroked(_object(points, False, settings, strokeWidth=2.0), settings)  # 2mm wide

    assert any(p.lineType == LineType.STROKE for p in obj.geometry), "nothing drawn at all"
    outside = _inkOutsideFootprint(obj, points, False, 2.0, settings)
    assert outside == pytest.approx(0.0, abs=1e-4), f"{outside:.5f} mm^2 of ink outside the true footprint"


def testSharpTurnWithShortTailPicksTheTailsOwnDirection(settings):
    """The cap plane at a path's end must be perpendicular to whichever segment
    actually meets that end - even when it's a short tail turning sharply off a much
    longer run - not to the run's own direction. _buttEndPlanes walks inward only far
    enough to find a non-coincident vertex, so a short tail's direction wins here.
    """
    # an 11mm vertical run, then a short 0.583mm tail turning off at a sharp angle
    vertices = [0 + 0j, 0 + 11j, 0.5 + 11.3j]
    tailDirection = (vertices[2] - vertices[1]) / abs(vertices[2] - vertices[1])

    planes = _buttEndPlanes(vertices, 1.0)
    assert len(planes) == 2
    farEnd, farTangent = planes[0]  # index len-1: the tail's own end
    assert farEnd == vertices[2]
    assert farTangent == pytest.approx(tailDirection)
    # the tail's direction is nowhere near the main run's (0+1j) - if this ever came
    # back close to that, the plane silently fell back to the wrong segment
    assert abs(farTangent - 1j) > 0.5


def testSharpTurnWithShortTailDoesNotCrashOrOverink(settings):
    """Pipeline-level companion to the direct _buttEndPlanes check above: the short
    tail must still get inked (not erased outright by a plane computed against the
    wrong direction) and, as with the degenerate-length case, the ink that IS drawn
    must stay within the true footprint.
    """
    points = [0 + 0j, 0 + 11j, 0.5 + 11.3j]
    obj = _stroked(_object(points, False, settings, strokeWidth=2.0), settings)

    outside = _inkOutsideFootprint(obj, points, False, 2.0, settings)
    assert outside == pytest.approx(0.0, abs=1e-4), f"{outside:.5f} mm^2 of ink outside the true footprint"

    # ink actually reaches near the tail tip - if the cap plane had used the main
    # run's direction instead, it would have sheared the tail off entirely
    tip = points[-1]
    nearTip = [_toClipperPath([tip + 1 + 1j, tip + 1 - 1j, tip - 1 - 1j, tip - 1 + 1j])]
    ink = _inkPolys(obj, settings, roles=(LineType.STROKE,))
    assert _area(_intersect(ink, nearTip)) > 0.1, "no ink survived near the short tail's tip"


def testAnAlmostClosedRingGetsNoCapSetback(settings):
    """A circle drawn as an arc that stops a hair short of its own start is open, so it
    has two butt caps - but they sit closer together than the stroke is wide, so
    offsetting seals them into a continuous ring with no cap left anywhere on it. Their
    planes by then face each other, and a cutter built from them reaches clear across the
    shape rather than just past an end, cutting a spur into the middle of the drawing.
    """
    radius, gap = 5.0, 0.01
    # left as an Arc for the pipeline to flatten: the coarse polygon tolerance allows is
    # what puts the offsetter's near-coincident edges where the boolean goes wrong
    arc = Arc(center=0j, u=complex(radius, 0), v=complex(0, -radius),
              t0=0.0, sweep=math.tau - gap / radius)
    path = Path([arc])
    path.lineType = LineType.RAW_GEOMETRY
    obj = _stroked(PathObject("ring", [path], Style(strokeColor=[0, 0, 0], strokeWidth=0.3,
                                                    fillColor=None), Transform()), settings)

    strokes = [p for p in obj.geometry if p.lineType == LineType.STROKE]
    assert strokes, "the ring got no stroke passes"
    for p in strokes:
        drawn = p.tessellate(settings.tessellationTolerance, allowArcs=False).vertices()
        innermost = min(abs(q) for q in drawn)
        assert innermost > radius - 0.3, (
            f"a pass dived to r={innermost:.3f} inside a ring at r={radius} - that's the "
            f"cap cutter reaching across the shape"
        )


def testHairpinEndsKeepTheirCapPlanes():
    """What marks the almost-closed ring above is its two end tangents opposing each
    other, not just its two ends being close. A hairpin's caps sit within a stroke width
    of each other too, but they head the same way and are both real caps still owed a
    setback - so closeness alone must not be enough to drop them."""
    hairpin = [0 + 0j, 10 + 0j, 10 + 0.2j, 0 + 0.2j]
    assert abs(hairpin[-1] - hairpin[0]) < 2.0, "fixture no longer has its ends close"
    assert len(_buttEndPlanes(hairpin, 2.0)) == 2


#endregion

#region the dash walk


# each length is a whole number of the pattern's periods, so the duty cycle applies
# exactly. (It only holds then - 12mm of [4,1,1,1] is 1.7 periods and the leftover
# tail happens to be all "on", giving 9mm rather than 12 x 5/7.)
@pytest.mark.parametrize("pattern, length, duty", [
    ([2.0, 2.0], 12.0, 1 / 2),
    ([3.0, 1.0], 12.0, 3 / 4),
    ([1.0, 3.0], 12.0, 1 / 4),
    ([4.0, 1.0, 1.0, 1.0], 14.0, 5 / 7),
    # what Style.dashPattern() hands over for an odd-length "5 3 2"
    ([5.0, 3.0, 2.0, 5.0, 3.0, 2.0], 20.0, 1 / 2),
])
def testDashInkMatchesTheOnFraction(pattern, length, duty, settings):
    """Total inked length is the pattern's duty cycle times the path length.

    The sharp version of "dashes work": a walk that drifts, double-counts a segment
    boundary or mis-orders the on/off phases lands on a different number here, where
    "the output changed" would not notice.
    """
    line = Path.fromPoints([0 + 0j, length + 0j], closed=False)
    dashes = _applyDash(line, pattern, 0, settings.tessellationTolerance)
    assert _inkLength(dashes) == pytest.approx(length * duty)
    assert all(not d.isClosed() for d in dashes), "a dash of an open path must be open"


def testZeroGapPatternIsOneContinuousDash(settings):
    """"5 0" has zero-length gaps, so it inks the whole path - and should come back
    as a single dash, not a row of pieces meeting at a point."""
    line = Path.fromPoints(LINE_PTS, closed=False)
    dashes = _applyDash(line, [5.0, 0.0], 0, settings.tessellationTolerance)
    assert len(dashes) == 1
    assert _inkLength(dashes) == pytest.approx(12.0)


def testDashesPreserveArcs(settings):
    """A dashed arc must stay an arc"""
    circle = Path([Arc(center=0 + 0j, u=10 + 0j, v=0 + 10j, t0=0, sweep=2 * math.pi)])
    dashes = _applyDash(circle, [2.0, 2.0], 0, settings.tessellationTolerance)
    kinds = {type(s).__name__ for d in dashes for s in d.segments}
    assert kinds == {"Arc"}, f"expected only Arcs, got {kinds}"
    # 62.83mm circumference / 4mm period = 15.7 periods, so 16 on-spans, the last
    # one clipped short by the path end
    assert _inkLength(dashes) == pytest.approx(2.0 * 15 + 2.0)


#endregion

#region dash offset


def testDashOffsetMovesWhereDashesStart(settings):
    """A positive offset shifts the pattern forward along the path."""
    line = Path.fromPoints(LINE_PTS, closed=False)
    tol = settings.tessellationTolerance

    atZero = _applyDash(line, [2.0, 2.0], 0, tol)
    atOne = _applyDash(line, [2.0, 2.0], 1, tol)

    # offset 0 starts inked at the path start; offset 1 is 1mm into that first dash,
    # so its opening dash is only 1mm long and the next starts 1mm earlier
    assert atZero[0].start() == pytest.approx(0 + 0j)
    assert atZero[0].length() == pytest.approx(2.0)
    assert atOne[0].length() == pytest.approx(1.0)
    assert atOne[1].start().real == pytest.approx(3.0)


def testNegativeDashOffsetShiftsBackwards(settings):
    """SVG 2 reads a negative offset as shifting the pattern backwards, which means
    the path starts in a GAP here rather than being rejected as invalid."""
    line = Path.fromPoints(LINE_PTS, closed=False)
    dashes = _applyDash(line, [2.0, 2.0], -1, settings.tessellationTolerance)
    assert dashes[0].start().real == pytest.approx(1.0), "expected to start 1mm in, mid-gap"


def testDashOffsetPreservesTotalInkOnAClosedPath(settings):
    """On a loop whose perimeter is a whole number of periods, sliding the offset
    moves the dashes but must not create or destroy ink."""
    square = Path.fromPoints(SQUARE_PTS, closed=True)  # perimeter 40 = 10 x [2,2]
    tol = settings.tessellationTolerance
    inks = [_inkLength(_applyDash(square, [2.0, 2.0], off, tol)) for off in (0, 0.5, 1, 1.7, 3)]
    assert all(ink == pytest.approx(20.0) for ink in inks), inks


def testClosedPathMergesTheSeamDash(settings):
    """A dash running across a closed path's start point is ONE dash in SVG, joined
    there rather than capped twice."""
    square = Path.fromPoints(SQUARE_PTS, closed=True)
    dashes = _applyDash(square, [2.0, 2.0], 1, settings.tessellationTolerance)

    # 11 spans (the pattern is mid-dash at both ends) collapse to 10 real dashes
    assert len(dashes) == 10
    assert all(d.length() == pytest.approx(2.0) for d in dashes)

    # the merged one wraps the corner at the origin: it starts up the left edge and
    # ends along the bottom, so it spans two edges rather than lying on one
    seam = dashes[0]
    assert len(seam.segments) == 2
    assert seam.start() == pytest.approx(0 + 1j)
    assert seam.end() == pytest.approx(1 + 0j)


#endregion

#region dashes in the pipeline


def testDashedStrokeInksLessThanSolid(settings):
    """The split is actually wired into generateStroke, not just available."""
    solid = _stroked(_object(LINE_PTS, False, settings), settings)
    dashed = _stroked(_object(LINE_PTS, False, settings, dasharray=[2.0, 2.0]), settings)

    solidArea = _area(_inkPolys(solid, settings))
    dashedArea = _area(_inkPolys(dashed, settings))
    assert dashedArea < solidArea, f"dashed {dashedArea:.3f} not less than solid {solidArea:.3f}"


@pytest.mark.parametrize("a, b", [
    ({"dasharray": [2.0, 2.0]}, {"dasharray": [4.0, 1.0]}),          # pattern differs
    ({"dasharray": [2.0, 2.0]}, {"dasharray": [2.0, 2.0], "dashoffset": 1.0}),  # offset differs
])
def testDifferentDashSettingsProduceDifferentGeometry(a, b, settings):
    def signature(style):
        obj = _stroked(_object(LINE_PTS, False, settings, **style), settings)
        return sorted(
            (round(v.real, 4), round(v.imag, 4))
            for p in obj.geometry if p.lineType in DRAWN
            for v in p.tessellate(settings.tessellationTolerance, allowArcs=False).vertices()
        )
    assert signature(a) != signature(b)


def testDashesStayInsideTheSolidBand(settings):
    """Dashing removes ink, it never adds any outside where a solid stroke would go."""
    solid = _stroked(_object(SQUARE_PTS, True, settings), settings)
    dashed = _stroked(_object(SQUARE_PTS, True, settings, dasharray=[3.0, 2.0]), settings)

    from lib.infill import _offsetPolys
    solidInk = _inkPolys(solid, settings)
    dashedInk = _inkPolys(dashed, settings)

    pc = pyclipper.Pyclipper()
    pc.AddPaths(dashedInk, pyclipper.PT_SUBJECT, True)
    pc.AddPaths(solidInk, pyclipper.PT_CLIP, True)
    outside = pc.Execute(pyclipper.CT_DIFFERENCE, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)

    # Judge by THICKNESS, not area - same reasoning as test_coverage.py's
    # GAP_HALF_WIDTH_FRACTION. A dash end is offset as an open path (one contour
    # wrapping both sides) where the solid square's corner is a closed-path miter,
    # so the two disagree by a few tens of microns at each of the 16 dash ends.
    # Genuine stray ink would be a whole pen pass - fillSpacing wide, an order of
    # magnitude above that - so eroding by a quarter of it separates the two cleanly.
    thick = _offsetPolys(outside, -settings.fillSpacing / 4) if outside else []
    assert not thick, (
        f"{_area(thick):.4f} mm^2 of dashed ink lies outside the solid band by more "
        f"than a tessellation tolerance"
    )


def testFallbackStillDashes(settings):
    """generateStroke=False draws plain centrelines - but they still have to be the
    dashes, not the whole outline. Guards the hook sitting before that branch."""
    settings.generateStroke = False
    obj = _stroked(_object(LINE_PTS, False, settings, dasharray=[2.0, 2.0]), settings)

    drawn = [p for p in obj.geometry if p.lineType == LineType.STROKE]
    assert len(drawn) == 3, "expected one centreline per dash"
    assert _inkLength(drawn) == pytest.approx(6.0)


#endregion

#region one-sided


def _inkInsideFill(obj, settings):
    """mm^2 of the object's STROKE ink that lands inside its own fill region.

    The fill region is eroded by a tessellation tolerance first, so ink sitting
    exactly ON the centreline (which is the boundary, and is where the outward half
    is supposed to stop) doesn't register as being inside it.
    """
    from lib.infill import _offsetPolys, _resolveFillRegion
    region = _resolveFillRegion(obj, settings.tessellationTolerance)
    if not region:
        return 0.0
    interior = _offsetPolys(region, -settings.tessellationTolerance)
    return _area(_intersect(_inkPolys(obj, settings, roles=(LineType.STROKE,)), interior))


def testDashedStrokeOverFillDrawsOnlyItsOutwardHalf(settings):
    """The inner half is the fill's job, so no stroke ink may land in the interior."""
    obj = _object(SQUARE_PTS, True, settings, dasharray=[3.0, 2.0], fillColor=[0, 0, 0])
    _stroked(obj, settings, withFill=True)

    assert _inkInsideFill(obj, settings) == pytest.approx(0.0, abs=0.01)
    # and the fill really did run - otherwise "nothing inside" is trivially true
    # because the interior was never drawn at all
    assert any(p.lineType == LineType.INFILL for p in obj.geometry), "no infill generated"


def testDashedStrokeWithoutFillStaysTwoSided(settings):
    """The guard: with no fill, nothing else covers the inner half, so the stroke
    must still straddle the centreline.

    Same shape as the test above with fill turned off - if one-siding leaked into
    this case, the inner half of every dash would go uninked.
    """
    obj = _stroked(_object(SQUARE_PTS, True, settings, dasharray=[3.0, 2.0]), settings)

    # measure against the square's own interior, since there's no fill region now
    interior = [_toClipperPath([complex(p.real, p.imag) for p in SQUARE_PTS])]
    inside = _area(_intersect(_inkPolys(obj, settings, roles=(LineType.STROKE,)), interior))
    assert inside > 1.0, f"only {inside:.3f} mm^2 of ink inside - dashes were one-sided"


def testUnfillableDashedShapeStaysTwoSided(settings):
    """fillColor set but the path encloses no area, so no fill will be generated -
    the stroke has to stay two-sided even though the object claims a fill."""
    obj = _stroked(_object(LINE_PTS, False, settings, dasharray=[2.0, 2.0], fillColor=[0, 0, 0]), settings)

    ink = _area(_inkPolys(obj, settings, roles=(LineType.STROKE,)))
    # a two-sided 2mm-wide stroke over 6mm of dash covers ~12mm^2; a one-sided one
    # would be about half that
    assert ink > 9.0, f"only {ink:.3f} mm^2 of ink (~12 mm^2 expected) - the dashes were one-sided"


#endregion
