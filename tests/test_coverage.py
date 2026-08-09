"""The coverage invariant: everything the SVG asks to be inked, gets inked.

Method, per PathObject:
  target   = the region SVG would paint - the resolved fill region (per
             fill-rule) plus the stroke band (raw outline offset by
             strokeWidth/2 with the object's own join/cap/miterlimit)
  ink      = every drawn subpath's centerline swept by +/- fillSpacing/2
  uncovered = target - ink, opened at `tolerance` to discard the hairline
             seams that boolean ops on tessellated geometry always leave, then
             eroded by GAP_HALF_WIDTH_FRACTION * fillSpacing - whatever survives
             that is a gap too thick to blame on discretization

Overcoverage is fine - a doubled pen pass is invisible on paper. Undercoverage
is the bug, so only the one direction is asserted.

The target region is deliberately recomputed here rather than reusing
generateInfill's own resolution: a test that calls the code under test to decide
what the answer should be cannot detect that code being wrong. Only the low-level
clipper primitives are shared.
"""
import copy
import os
import pytest

pyclipper = pytest.importorskip("pyclipper", reason="coverage measurement needs pyclipper")

from lib.geometry import Line
from lib.settings import LineType
from lib.stroke import generateStroke
from lib.infill import generateInfill, _SCALE, _toClipperPath, _coverageBand, _difference, _offsetPolys, _joinType
from lib.svgparse import loadSvg, parseSvg

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
DRAWN_ROLES = (LineType.STROKE, LineType.INFILL, LineType.GAP_INFILL)

# A gap is only failed if it is thicker than this fraction of fillSpacing
# (measured as half-width, so 0.25 means "a quarter of a ring pitch to each side").
#
# Some leftover is expected and benign. Adjacent rings meet with a seam of a few
# tens of microns, and fillSpacing is deliberately set under the real pen width
# precisely so the pen bridges those - CLAUDE.md calls these "the benign sub-penWidth
# specks between adjacent rings".
#
# So this is a KNOWN-ISSUES BASELINE, not a claim that such gaps are correct - it
# sits just above the current worst case so the suite is green today and any new
# or larger gap fails immediately.
GAP_HALF_WIDTH_FRACTION = 0.25

# Fixtures worth measuring. comprehensive.svg is the feature matrix; the others
# are real drawings whose organic curves produce residue shapes the hand-authored
# fixtures don't. horse.svg is not in the repo (uncertain license), so it is
# skipped rather than required - it measures fine locally if you have it.
COVERAGE_SVGS = [
    os.path.join(TESTS_DIR, "comprehensive.svg"),
    os.path.join(os.path.dirname(TESTS_DIR), "testDrawing.svg"),
    os.path.join(os.path.dirname(TESTS_DIR), "horse.svg"),
]


def _fillRegionOf(rawSubpaths, style, tolerance):
    """The area SVG would flood with fill, resolved under the object's fill-rule."""
    if style.fillColor is None:
        return []
    fillable = [p for p in rawSubpaths if p.isFillable()]
    if not fillable:
        return []
    paths = []
    for p in fillable:
        q = copy.deepcopy(p)
        if not q.isClosed():
            q.segments.append(Line(q.end(), q.start()))
        pts = _toClipperPath(q.tessellate(tolerance, allowArcs=False).vertices())
        if len(pts) >= 3:
            paths.append(pts)
    if not paths:
        return []
    fillType = pyclipper.PFT_EVENODD if style.fillRule == "evenodd" else pyclipper.PFT_NONZERO
    pc = pyclipper.Pyclipper()
    pc.AddPaths(paths, pyclipper.PT_SUBJECT, True)
    return pc.Execute(pyclipper.CT_UNION, fillType, fillType)


def _strokeBandOf(rawSubpaths, style, tolerance):
    """The area SVG would paint as stroke - the outline grown by strokeWidth/2."""
    if style.strokeColor is None or style.strokeWidth <= 0:
        return []
    band = []
    capType = {"round": pyclipper.ET_OPENROUND, "square": pyclipper.ET_OPENSQUARE}.get(
        style.linecap, pyclipper.ET_OPENBUTT
    )
    for p in rawSubpaths:
        pts = _toClipperPath(p.tessellate(tolerance, allowArcs=False).vertices())
        if len(pts) < 2:
            continue
        pco = pyclipper.PyclipperOffset()
        pco.MiterLimit = style.miterlimit
        endType = pyclipper.ET_CLOSEDLINE if p.isClosed() else capType
        pco.AddPath(pts, _joinType(style.linejoin), endType)
        band.extend(pco.Execute(style.strokeWidth / 2 * _SCALE))
    return band


def _union(a, b):
    if not a:
        return b
    if not b:
        return a
    pc = pyclipper.Pyclipper()
    pc.AddPaths(a, pyclipper.PT_SUBJECT, True)
    pc.AddPaths(b, pyclipper.PT_CLIP, True)
    return pc.Execute(pyclipper.CT_UNION, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)


def _area(paths) -> float:
    """Net mm^2 enclosed by a clipper result.

    Signed, deliberately. Execute returns outer contours wound one way and holes
    the other, so summing the signed areas subtracts holes correctly. Summing
    abs() instead would add holes instead of subtracting them.
    """
    return sum(pyclipper.Area(c) for c in paths) / (_SCALE ** 2)

def _uncoveredArea(target, drawnPaths, spacing, tolerance):
    """mm^2 of target no drawn centerline's ink band reaches, and how thick it is.

    Returns (area, pieceCount, thickArea) where thickArea counts only what
    survives eroding by GAP_HALF_WIDTH_FRACTION * spacing - see that constant.
    """
    if not target:
        return 0.0, 0, 0.0
    if not drawnPaths:
        whole = _area(target)
        return whole, len(target), whole

    leftover = _difference(target, [_coverageBand(drawnPaths, spacing / 2)])
    if not leftover:
        return 0.0, 0, 0.0

    # Morphological open to drop hairline seams that are caused by numerical noise.
    # Opening at `tolerance` (not tolerance/2 - measured seams reach 10um,
    # which survives a 6um erosion) removes anything thinner than the geometry's
    # own representation error, which is the most that can be meaningfully resolved.
    eroded = _offsetPolys(leftover, -tolerance)
    leftover = _offsetPolys(eroded, tolerance) if eroded else []
    if not leftover:
        return 0.0, 0, 0.0

    # anything still standing after eroding by the budget is thicker than it.
    # this subsumes an area floor: a piece too small to matter is also too thin
    # to survive, so no separate minimum-area filter is needed
    thick = _offsetPolys(leftover, -spacing * GAP_HALF_WIDTH_FRACTION)
    thickArea = _area(thick) if thick else 0.0
    # count only outer contours - a hole is part of its parent piece, not a piece
    pieceCount = sum(1 for c in leftover if pyclipper.Area(c) > 0)
    return _area(leftover), pieceCount, thickArea


def _measure(svgPath, settings):
    """Runs stroke+infill and returns [(objId, area, pieceCount, thickArea), ...]."""
    document = parseSvg(loadSvg(svgPath), settings, 1, 1)
    spacing, tolerance = settings.fillSpacing, settings.tessellationTolerance

    # snapshot the raw centerlines before generation appends to obj.geometry
    rawByObj = {
        id(obj): [copy.deepcopy(p) for p in obj.geometry if p.lineType == LineType.RAW_GEOMETRY]
        for obj in document.objects
    }

    generateStroke(document, settings)
    generateInfill(document, settings)

    results = []
    for obj in document.objects:
        raw = rawByObj[id(obj)]
        if not raw:
            continue
        target = _union(
            _fillRegionOf(raw, obj.style, tolerance),
            _strokeBandOf(raw, obj.style, tolerance),
        )
        if not target:
            continue
        drawn = []
        for p in obj.geometry:
            if p.lineType in DRAWN_ROLES:
                pts = _toClipperPath(p.tessellate(tolerance, allowArcs=False).vertices())
                if len(pts) >= 2:
                    drawn.append(pts)
        area, pieces, thick = _uncoveredArea(target, drawn, spacing, tolerance)
        results.append((str(obj.id), area, pieces, thick))
    return results


@pytest.mark.slow
@pytest.mark.parametrize("svgPath", COVERAGE_SVGS, ids=os.path.basename)
def testNothingLeftUninked(svgPath, settings):
    """No object leaves a meaningful area of its own fill/stroke uninked."""
    if not os.path.isfile(svgPath):
        pytest.skip(f"{os.path.basename(svgPath)} not present")

    results = _measure(svgPath, settings)
    assert results, "no measurable objects - the fixture or the parser changed"

    failures = [(name, area, n, thick) for name, area, n, thick in results if thick > 0]
    detail = "\n".join(
        f"  {name}: {thick:.4f} mm^2 thicker than "
        f"{settings.fillSpacing * GAP_HALF_WIDTH_FRACTION:.4f}mm half-width "
        f"({area:.4f} mm^2 uncovered total, {n} piece(s))"
        for name, area, n, thick in failures[:15]
    )
    assert not failures, (
        f"{len(failures)} of {len(results)} objects left ink gaps wider than the "
        f"known-issues budget in {os.path.basename(svgPath)}:\n{detail}"
    )


@pytest.mark.slow
def testGapInfillIsWhatClosesTheGaps(settings):
    """Disabling generateGapInfill should make coverage strictly worse."""
    svgPath = os.path.join(TESTS_DIR, "comprehensive.svg")
    withGap = sum(area for _, area, _, _ in _measure(svgPath, settings))

    settings.generateGapInfill = False
    withoutGap = sum(area for _, area, _, _ in _measure(svgPath, settings))

    assert withoutGap > withGap, (
        f"gap infill contributed nothing: {withoutGap:.4f} mm^2 uncovered without it "
        f"vs {withGap:.4f} mm^2 with it"
    )
