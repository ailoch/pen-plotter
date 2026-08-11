"""Unit-level tests for lib/infill.py.

The bulk of infill's correctness is measured in test_coverage.py - that's the
property (nothing left uninked) rather than the mechanism. What lives here is
behaviour that has a specific, checkable right answer.
"""
import pytest

pyclipper = pytest.importorskip("pyclipper", reason="infill needs pyclipper")

from lib.geometry import Document, Path, PathObject, Style, Transform
from lib.infill import generateInfill
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
