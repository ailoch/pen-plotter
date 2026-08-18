"""Unit-level tests for lib/infill.py.

The bulk of infill's correctness is measured in test_coverage.py - that's the
property (nothing left uninked) rather than the mechanism. What lives here is
behaviour that has a specific, checkable right answer.
"""
import time

import pytest

pyclipper = pytest.importorskip("pyclipper", reason="infill needs pyclipper")

from lib.geometry import Document, Path, PathObject, Style, Transform
from lib.infill import (_MEDIAL_SAMPLE_DIVISOR, _SCALE, _coverageBand, _difference,
                        _drawResidue, _medialAxisLines, _offsetPolys, _toClipperPath,
                        generateInfill)
from lib.settings import LineType

DRAWN = (LineType.INFILL, LineType.GAP_INFILL)

# A square ring inside a larger square, both wound the SAME direction. This is
# the canonical fill-rule discriminator:
#   nonzero  - the inner square's winding adds to the outer's (both +1, total 2),
#              a nonzero total, so the middle is FILLED and the shape is solid
#   evenodd  - a ray from the middle crosses two edges, an even count, so the
#              middle is a HOLE and the shape is a donut
# Wound the other way, nonzero would cancel to 0 and both rules would agree -
# which is exactly why the winding direction here matters and is not incidental.
OUTER = [0 + 0j, 20 + 0j, 20 + 20j, 0 + 20j]
INNER = [5 + 5j, 15 + 5j, 15 + 15j, 5 + 15j]

#region helpers


def _donut(fillRule: str) -> PathObject:
    outer = Path.fromPoints(OUTER, closed=True)
    inner = Path.fromPoints(INNER, closed=True)
    for p in (outer, inner):
        p.lineType = LineType.RAW_GEOMETRY
    style = Style(fillColor=[0, 0, 0], fillRule=fillRule, strokeColor=None)
    return PathObject(f"donut-{fillRule}", [outer, inner], style, Transform())


def _filledDonut(fillRule: str, settings) -> PathObject:
    obj = _donut(fillRule)
    document = Document()
    document.add(obj)
    generateInfill(document, settings)
    return obj


def _inkPoints(obj, tolerance) -> list[complex]:
    """Every vertex of every drawn subpath - where ink actually lands."""
    pts = []
    for p in obj.geometry:
        if p.lineType in DRAWN:
            pts.extend(p.tessellate(tolerance, allowArcs=False).vertices())
    return pts


def _inHole(pt: complex) -> bool:
    """Strictly inside the inner square."""
    return 5 < pt.real < 15 and 5 < pt.imag < 15


def _wedge() -> PathObject:
    """A long acute triangle: too thin for the ring tiling to reach the tip, so it
    always leaves residue for the gap fill to deal with."""
    return PathObject("wedge", [Path.fromPoints([0 + 0j, 40 + 0j, 40 + 3j], closed=True)],
                      Style(fillColor=[0, 0, 0], strokeColor=None), Transform())


def _square(id: str, size: float = 20.0, overrides: dict | None = None) -> PathObject:
    outline = Path.fromPoints([0j, complex(size, 0), complex(size, size), complex(0, size)], closed=True)
    return PathObject(id, [outline], Style(fillColor=[0, 0, 0], strokeColor=None), Transform(), overrides=overrides or {})


#endregion


def testEvenoddLeavesTheHoleEmpty(settings):
    """Under evenodd the inner square is a hole - no ink may land inside it."""
    obj = _filledDonut("evenodd", settings)
    pts = _inkPoints(obj, settings.tessellationTolerance)
    # without this, an infill that produced nothing at all would satisfy
    # "no ink in the hole" and pass while being completely broken
    assert pts, "evenodd produced no infill at all"

    inside = [p for p in pts if _inHole(p)]
    assert not inside, (
        f"{len(inside)} ink point(s) inside the evenodd hole, e.g. {inside[:3]}"
    )


def testNonzeroFillsTheHole(settings):
    """Under nonzero the same outline is solid - the middle must be inked."""
    obj = _filledDonut("nonzero", settings)
    inside = [p for p in _inkPoints(obj, settings.tessellationTolerance) if _inHole(p)]
    assert inside, "nonzero should fill the centre, but no ink landed inside it"


def testThinResidueIsFilledWithMedialAxisStrokes(settings):
    """A piece too thin to survive a spacing/2 erosion is drawn as its skeleton -
    open centreline strokes, one pen pass, rather than the doubled-back loops a
    concentric fill would leave. (Without scipy this falls back to those loops
    instead; that path is test_fallbacks.py's.)"""
    pytest.importorskip("scipy.spatial", reason="the medial axis needs scipy")
    obj = _wedge()
    document = Document()
    document.add(obj)
    generateInfill(document, settings)

    gapFill = [p for p in obj.geometry if p.lineType == LineType.GAP_INFILL]
    assert gapFill, "an acute wedge should leave residue for the gap fill"
    assert not any(p.isClosed() for p in gapFill)


def testATaperedResiduePieceIsInkedAlongItsWholeLength(settings):
    """A piece counts as "wide" - loops rather than skeleton - as soon as it survives a
    spacing/2 erosion ANYWHERE. Where it then tapers under that width the inset those
    loops come from has already vanished, so the loops ink only the fat end and the rest
    of the piece would be left bare unless the shortfall is skeletonised too."""
    pytest.importorskip("scipy.spatial", reason="the medial axis needs scipy")
    spacing, tolerance = settings.fillSpacing, settings.tessellationTolerance
    length = 30.0
    fat, thin = spacing * 1.2, spacing * 0.7
    wedge = _toClipperPath([
        complex(0, -fat / 2), complex(length, -thin / 2),
        complex(length, thin / 2), complex(0, fat / 2),
    ])

    geometry: list = []
    _drawResidue(geometry, [wedge], spacing, tolerance, "wedge")
    assert geometry, "the wedge got no gap fill at all"

    drawn = [_toClipperPath(p.tessellate(tolerance, allowArcs=False).vertices()) for p in geometry]
    missed = _difference([wedge], [_coverageBand([d for d in drawn if len(d) >= 2], spacing / 2)])
    # opened at tolerance, as everywhere else: a boolean on tessellated geometry always
    # leaves hairline slivers along the shared boundary
    missed = _offsetPolys(missed, -tolerance) if missed else []
    missedArea = sum(abs(pyclipper.Area(c)) for c in missed) / _SCALE ** 2
    wedgeArea = abs(pyclipper.Area(wedge)) / _SCALE ** 2
    assert missedArea < wedgeArea * 0.05, (
        f"{missedArea:.3f} of {wedgeArea:.3f} mm^2 left uninked - the taper went bare"
    )


def testALongStraightSliverSkeletonisesQuickly(settings):
    """Resampling a straight edge lays down a run of exactly-collinear sites, and qhull
    goes quadratic on those unless they're nudged apart first. Rather than a hardcoded
    wall-clock budget (flaky across machines), this times a raw Voronoi call on
    randomly-scattered points of the same count as a baseline for well-behaved input at
    this scale, then checks the real call against it - proportional, not absolute, so
    only a return of the collinear-site blowup (two orders of magnitude, not machine
    noise) trips it."""
    np = pytest.importorskip("numpy")
    Voronoi = pytest.importorskip("scipy.spatial").Voronoi
    long, thin = round(180 * _SCALE), round(0.1 * _SCALE)
    sliver = [(0, 0), (long, 0), (long, thin), (0, thin)]
    spacing = 0.15

    # matches _medialAxisLines' own resampling density for this sliver's perimeter
    maxEdge = max(spacing / _MEDIAL_SAMPLE_DIVISOR * _SCALE, 1.0)
    sampleCount = round(2 * (long + thin) / maxEdge)
    baselinePoints = np.random.default_rng(0).uniform(0, max(long, thin), (sampleCount, 2))
    start = time.perf_counter()
    Voronoi(baselinePoints)
    baseline = time.perf_counter() - start

    start = time.perf_counter()
    lines = _medialAxisLines(sliver, [], spacing, settings.tessellationTolerance)
    elapsed = time.perf_counter() - start

    assert lines, "a sliver this thin should skeletonise to centreline strokes"
    budget = max(baseline * 10, 1)
    assert elapsed < budget, (
        f"took {elapsed:.2f}s against a {baseline:.2f}s well-behaved baseline of the "
        f"same point count - the collinear-site blowup is back"
    )


#region fillSpacing override


def testFillSpacingOverrideReplacesSettingsFillSpacingForThatObject(settings):
    """A per-object override lets one shape preview a spacing the rest of the document
    isn't using - the whole reason the spacing calibration sheet can fill several
    blocks, each at a different pitch, through one ordinary generateInfill call."""
    tight = _square("tight", overrides={"fillSpacing": settings.fillSpacing / 3})
    plain = _square("plain")
    document = Document()
    document.add(tight)
    document.add(plain)
    generateInfill(document, settings)

    tightInk = sum(p.length() for p in tight.geometry if p.lineType in DRAWN)
    plainInk = sum(p.length() for p in plain.geometry if p.lineType in DRAWN)
    assert tightInk > plainInk, "the tighter override should draw denser rings"


def testFillSpacingOverrideIsConsumedAndRemovedFromOverrides(settings):
    """Left behind, it would sit unrecognized in the object's overrides and trip
    _addPath's unknown-override warning at draw time - fillSpacing only means anything
    during infill generation."""
    obj = _square("sq", overrides={"fillSpacing": settings.fillSpacing / 2})
    document = Document()
    document.add(obj)
    generateInfill(document, settings)
    assert "fillSpacing" not in obj.overrides


def testZeroFillSpacingOverrideDisablesFillForJustThatObject(settings):
    off = _square("off", overrides={"fillSpacing": 0})
    on = _square("on")
    document = Document()
    document.add(off)
    document.add(on)
    generateInfill(document, settings)

    assert not any(p.lineType in DRAWN for p in off.geometry)
    assert any(p.lineType in DRAWN for p in on.geometry)


#endregion
