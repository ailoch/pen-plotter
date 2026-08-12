"""What the pipeline does when an optional dependency isn't installed.

pyclipper and scipy/numpy are both imported defensively - stroke/infill degrade
to a simpler output rather than crashing.

The two are patched out differently because they're imported differently.
pyclipper is imported once at module load and bound as a module attribute, so
overwriting that attribute is exactly what a missing install looks like from the
code's point of view. scipy/numpy are imported lazily inside _medialAxisLines,
which re-runs the import statement on every call - there's no attribute to
overwrite, so the import itself has to fail, which is what a None entry in
sys.modules does.
"""
import sys

import pytest

import lib.infill as infill
import lib.stroke as stroke
from lib.geometry import Arc, CubicBezier, Document, Line, Path, PathObject, Style
from lib.settings import LineType, Settings

BLACK = [0, 0, 0]

#region helpers


def _settings(**overrides) -> Settings:
    base = dict(fillSpacing=.3, tessellationTolerance=.012, penWidth=.5, generateGapInfill=True)
    return Settings(**{**base, **overrides})

def _document(*objects: PathObject) -> Document:
    doc = Document()
    for obj in objects:
        doc.add(obj)
    return doc

def _square(id: str, closed: bool = True, **style) -> PathObject:
    return PathObject(id, [Path.fromPoints([0 + 0j, 20 + 0j, 20 + 20j, 0 + 20j], closed)], Style(**style))

def _wedge() -> PathObject:
    """A long acute triangle: too thin for the ring tiling to reach the tip, so it
    always leaves residue for the gap fill to deal with."""
    return PathObject("wedge", [Path.fromPoints([0 + 0j, 40 + 0j, 40 + 3j], closed=True)],
                      Style(fillColor=BLACK, strokeColor=None))

def _roles(obj: PathObject, lineType: LineType) -> list[Path]:
    return [p for p in obj.geometry if p.lineType == lineType]

@pytest.fixture
def noPyclipper(monkeypatch):
    """Both modules bind their own `pyclipper` name, so both have to go."""
    monkeypatch.setattr(stroke, "pyclipper", None)
    monkeypatch.setattr(infill, "pyclipper", None)

@pytest.fixture
def noScipy(monkeypatch):
    """A None entry in sys.modules makes the import raise ImportError, which is what
    the lazy import inside _medialAxisLines catches."""
    for name in ("scipy.spatial", "numpy"):
        monkeypatch.setitem(sys.modules, name, None)


#endregion

#region no pyclipper: strokes


def testStrokesFallBackToASingleCenterlinePass(noPyclipper):
    """One pass per raw subpath, in place of the concentric family - the look the
    converter had before multi-pass expansion existed."""
    obj = _square("sq", strokeColor=BLACK, strokeWidth=2.0)
    stroke.generateStroke(_document(obj), _settings())
    assert len(_roles(obj, LineType.STROKE)) == 1

def testTheFallbackPassKeepsTheGeometryExactly(noPyclipper):
    """It's a deepcopy, not an offset - so arcs and beziers survive as themselves
    rather than being flattened into an offset polyline."""
    raw = Path([Line(0 + 0j, 10 + 0j),
                Arc(center=10 + 10j, u=0 - 10j, v=10 + 0j, t0=0, sweep=1.5708),
                CubicBezier(20 + 10j, 22 + 14j, 26 + 14j, 28 + 10j)], LineType.RAW_GEOMETRY)
    obj = PathObject("curvy", [raw], Style(strokeColor=BLACK, strokeWidth=2.0))
    stroke.generateStroke(_document(obj), _settings())

    centerPass = _roles(obj, LineType.STROKE)[0]
    assert [type(s) for s in centerPass.segments] == [Line, Arc, CubicBezier]
    assert centerPass.segments[0] is not raw.segments[0], "a copy, so later passes can't alias it"

def testTheFallbackLeavesTheRawGeometryAlone(noPyclipper):
    """generateInfill runs next and still needs the untouched centerline."""
    obj = _square("sq", strokeColor=BLACK, strokeWidth=2.0)
    stroke.generateStroke(_document(obj), _settings())
    assert len(_roles(obj, LineType.RAW_GEOMETRY)) == 1

def testDashesAreStillAppliedInTheFallback(noPyclipper):
    """Dashing happens before the multi-pass branch, so losing pyclipper must not
    quietly turn a dashed outline back into a solid one."""
    obj = _square("sq", strokeColor=BLACK, strokeWidth=1.0, dasharray=[3.0, 3.0])
    stroke.generateStroke(_document(obj), _settings())

    dashes = _roles(obj, LineType.STROKE)
    assert len(dashes) > 1, "the outline came through as one piece - the dashes were dropped"
    inked = sum(sum(s.length() for s in p.segments) for p in dashes)
    assert inked == pytest.approx(80 * 0.5, abs=1.5), "a 3-on/3-off pattern inks half the perimeter"

def testTheMissingPyclipperWarningIsPrintedOncePerRun(noPyclipper, capsys):
    """Per run, not per object - the install is missing once, however many shapes
    are affected by it."""
    doc = _document(_square("a", strokeColor=BLACK, strokeWidth=2.0),
                    _square("b", strokeColor=BLACK, strokeWidth=2.0),
                    _square("c", strokeColor=BLACK, strokeWidth=2.0))
    stroke.generateStroke(doc, _settings())
    assert capsys.readouterr().out.count("pyclipper is not installed") == 1

def testAnUnstrokedShapeDrawsNothingInTheFallback(noPyclipper):
    """The no-stroke gate comes first, so a fill-only shape is unaffected."""
    obj = _square("sq", fillColor=BLACK)
    stroke.generateStroke(_document(obj), _settings())
    assert _roles(obj, LineType.STROKE) == []


#endregion

#region no pyclipper: infill


def testNoInfillIsGeneratedWithoutPyclipper(noPyclipper):
    obj = _square("sq", fillColor=BLACK)
    infill.generateInfill(_document(obj), _settings())
    assert _roles(obj, LineType.INFILL) == []
    assert _roles(obj, LineType.GAP_INFILL) == []

def testAnOpenFillableShapeIsStillClosedWithoutPyclipper(noPyclipper):
    """Closing the outline needs no clipper, and the drawn stroke has to match the
    shape SVG would have filled whether or not the fill itself makes it."""
    obj = _square("sq", closed=False, fillColor=BLACK)
    infill.generateInfill(_document(obj), _settings())

    outline = _roles(obj, LineType.RAW_GEOMETRY)[0]
    assert outline.isClosed()
    assert len(outline.segments) == 4, "the closing segment, not a re-traced outline"

def testTheMissingPyclipperInfillWarningIsPrintedOncePerRun(noPyclipper, capsys):
    doc = _document(_square("a", fillColor=BLACK), _square("b", fillColor=BLACK))
    infill.generateInfill(doc, _settings())
    assert capsys.readouterr().out.count("pyclipper is not installed") == 1

def testNoWarningWhenFillIsDisabledAnyway(noPyclipper, capsys):
    """fillSpacing <= 0 already means no fill, so the missing install changes nothing
    and there's nothing to report."""
    obj = _square("sq", fillColor=BLACK)
    infill.generateInfill(_document(obj), _settings(fillSpacing=0))
    assert "pyclipper is not installed" not in capsys.readouterr().out


#endregion

#region no scipy/numpy: gap fill


def testGapFillFallsBackToLoopsWithoutScipy(noScipy):
    """No Voronoi means no skeleton, so the residue is filled with concentric loops
    instead - more passes for the same ink, but nothing is left uninked. The
    medial-axis strokes this replaces are test_infill.py's."""
    pytest.importorskip("pyclipper", reason="residue detection needs pyclipper")
    obj = _wedge()
    infill.generateInfill(_document(obj), _settings())

    gapFill = _roles(obj, LineType.GAP_INFILL)
    assert gapFill, "the residue must still be filled, just differently"
    assert all(p.isClosed() for p in gapFill)


#endregion
