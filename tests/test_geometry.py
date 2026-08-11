"""Unit tests for lib/geometry.py.

geometry.py is the one module with no dependencies of its own, and everything
downstream is built on it - so a wrong answer here surfaces as a mystery further
down the pipeline. These tests all assert an exact, hand-checkable number rather
than "the output changed".

The tessellation fitter (_tryFitRange/_greedyExtent/_fitRange) is deliberately
NOT tested here: its contract is "within tolerance of the true curve", which is
what test_coverage.py already measures end-to-end. What is tested is the exact
arithmetic the fitter rests on - the primitives' point/derivative/bounds - plus
the non-obvious cases where a plausible-looking implementation is wrong.
"""
import copy
import math

import pytest

from lib.geometry import (
    Arc, CubicBezier, Document, Line, Path, PathObject, QuadraticBezier,
    Segment, Style, Transform,
)
from lib.settings import LineType

SQUARE_PTS = [0 + 0j, 10 + 0j, 10 + 10j, 0 + 10j]


#region Style.dashPattern


@pytest.mark.parametrize("dasharray, expected", [
    (None, None),                                        # solid - the SVG default
    ([], None),                                          # empty list is also solid
    ([5.0, 3.0], [5.0, 3.0]),                            # already even, passed through
    ([5.0], [5.0, 5.0]),                                 # odd length repeats to become even
    ([5.0, 3.0, 2.0], [5.0, 3.0, 2.0, 5.0, 3.0, 2.0]),   # per spec: 5-on 3-off 2-on 5-off...
    ([0.0, 0.0], None),                                  # all-zero renders solid per spec
    ([0.0], None),                                       # all-zero survives the odd-length repeat
    ([0.0, 4.0], [0.0, 4.0]),                            # a zero *entry* is kept; only all-zero collapses
])
def testDashPatternNormalization(dasharray, expected):
    """The SVG normalization: odd-length repeats, all-zero collapses to solid."""
    assert Style(dasharray=dasharray).dashPattern() == expected


def testDashPatternDoesNotMutateTheStyle():
    """Normalization returns a new list - repeating in place would double the
    pattern again on every call."""
    style = Style(dasharray=[5.0, 3.0, 2.0])
    style.dashPattern()
    assert style.dasharray == [5.0, 3.0, 2.0]
    assert style.dashPattern() == [5.0, 3.0, 2.0, 5.0, 3.0, 2.0], "a second call must agree with the first"


#endregion

#region Transform


def testDefaultTransformIsIdentity():
    assert Transform().matrix == [1, 0, 0, 1, 0, 0]
    assert Transform().apply(3 + 4j) == pytest.approx(3 + 4j)


def testTranslateMovesPointsButNotVectors():
    """applyVector is apply without the translation - a direction has no position."""
    t = Transform()
    t.translate(10, 5)
    assert t.apply(1 + 1j) == pytest.approx(11 + 6j)
    assert t.applyVector(1 + 1j) == pytest.approx(1 + 1j)


def testSingleArgumentTranslateAndScaleAreUniform():
    t = Transform()
    t.translate(5)
    assert t.apply(0j) == pytest.approx(5 + 5j)

    s = Transform()
    s.scale(2)
    assert s.apply(3 + 4j) == pytest.approx(6 + 8j)


@pytest.mark.parametrize("method, args, point, expected", [
    ("translate", (5, 0), 0j, 5 + 0j),      # y=0 must stay 0, not fall back to x
    ("translate", (0, 5), 0j, 0 + 5j),      # and symmetrically for x=0
    ("scale", (3, 1), 1 + 1j, 3 + 1j),      # sy=1 is a real (identity) scale
])
def testZeroSecondArgumentIsNotTreatedAsOmitted(method, args, point, expected):
    """An explicit 0 is a real value. A falsy check here would silently
    substitute the first argument and skew the result along one axis."""
    t = Transform()
    getattr(t, method)(*args)
    assert t.apply(point) == pytest.approx(expected)


def testScaleAcceptsADegenerateAxis():
    """scale(sx, 0) flattens onto a line - degenerate, but explicitly asked for."""
    t = Transform()
    t.scale(3, 0)
    assert t.apply(2 + 7j) == pytest.approx(6 + 0j)


def testRotateAboutOrigin():
    """+90 degrees maps +x to +y in the matrix's own frame."""
    t = Transform()
    t.rotate(90)
    assert t.apply(1 + 0j) == pytest.approx(0 + 1j)
    assert t.apply(0 + 1j) == pytest.approx(-1 + 0j)


def testRotateAboutAPointHoldsThatPointFixed():
    """The defining property of a rotation centre. Getting the pre/post
    translations backwards still rotates, and still looks plausible in a
    preview, but slides the whole shape by 2*centre."""
    t = Transform()
    t.rotate(90, 1, 0)
    assert t.apply(1 + 0j) == pytest.approx(1 + 0j), "the centre must not move"
    # a point one unit +x of the centre swings one unit +y of it
    assert t.apply(2 + 0j) == pytest.approx(1 + 1j)


def testRotateFullTurnIsIdentity():
    t = Transform()
    t.rotate(360, 3, -7)
    assert t.apply(5 + 2j) == pytest.approx(5 + 2j)


def testSkewShearsOneAxisOnly():
    x = Transform()
    x.skewX(45)
    assert x.apply(0 + 1j) == pytest.approx(1 + 1j)  # y displaces x by tan(45)*y
    assert x.apply(1 + 0j) == pytest.approx(1 + 0j)  # a point on the x axis is unmoved

    y = Transform()
    y.skewY(45)
    assert y.apply(1 + 0j) == pytest.approx(1 + 1j)
    assert y.apply(0 + 1j) == pytest.approx(0 + 1j)


def testFlipsNegateOneAxisEach():
    x = Transform()
    x.flipAlongX()
    assert x.apply(3 + 4j) == pytest.approx(-3 + 4j)

    y = Transform()
    y.flipAlongY()
    assert y.apply(3 + 4j) == pytest.approx(3 - 4j)


def testAccumulatedOperationsApplyInTheGlobalFrame():
    """Each call post-multiplies, so the newest operation is applied last -
    scale-then-translate offsets by the raw translation, not the scaled one."""
    t = Transform()
    t.scale(2)
    t.translate(10, 10)
    assert t.apply(1 + 1j) == pytest.approx(12 + 12j)  # (1,1)*2 = (2,2), then +(10,10)

    reverse = Transform()
    reverse.translate(10, 10)
    reverse.scale(2)
    assert reverse.apply(1 + 1j) == pytest.approx(22 + 22j)  # (1,1)+(10,10) = (11,11), then *2


def testMatmulAndMulComposeInOppositeOrders():
    """`a @ b` applies b first; `a * b` applies a first. Both return a new
    Transform rather than mutating either operand."""
    a = Transform([2, 0, 0, 2, 0, 0])   # scale 2
    b = Transform([1, 0, 0, 1, 10, 0])  # translate +10x

    assert (a @ b).apply(0j) == pytest.approx(20 + 0j)  # translate, then scale
    assert (a * b).apply(0j) == pytest.approx(10 + 0j)  # scale, then translate
    assert a.matrix == [2, 0, 0, 2, 0, 0], "operands must not be mutated"
    assert b.matrix == [1, 0, 0, 1, 10, 0]


def testComposeAcceptsARawMatrix():
    """A bare 6-element list/tuple coerces, so callers needn't wrap it."""
    a = Transform([2, 0, 0, 2, 0, 0])
    expected = (a @ Transform([1, 0, 0, 1, 10, 0])).matrix
    assert (a @ [1, 0, 0, 1, 10, 0]).matrix == expected
    assert (a @ (1, 0, 0, 1, 10, 0)).matrix == expected


@pytest.mark.parametrize("bad", ["nope", 5, None, [1, 2, 3], [1, 2, 3, 4, 5, 6, 7], ["a"] * 6])
def testComposeRejectsAnythingElse(bad):
    """Returning NotImplemented (rather than raising directly) lets Python
    produce the standard unsupported-operand TypeError."""
    with pytest.raises(TypeError):
        _ = Transform() @ bad


#endregion

#region the Segment interface


def testSegmentCannotBeInstantiated():
    """Segment declares the interface every primitive owes; leaving a method out
    has to fail at construction rather than at some later call site."""
    with pytest.raises(TypeError):
        Segment()  # type: ignore[abstract]


@pytest.mark.parametrize("segment", [
    Line(0 + 0j, 3 + 4j),
    Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=math.pi),
    QuadraticBezier(0 + 0j, 5 + 10j, 10 + 0j),
    CubicBezier(0 + 0j, 0 + 10j, 10 + 10j, 10 + 0j),
], ids=["line", "arc", "quadratic", "cubic"])
def testEveryPrimitiveImplementsTheWholeInterface(segment: Segment):
    """Calling each method proves it's concrete - an inherited abstract stub
    would have blocked construction, and a missing override would raise here."""
    assert segment.length() > 0
    assert isinstance(segment.point(0.5), complex)
    assert isinstance(segment.derivative(0.5), complex)
    assert isinstance(segment.extrema(), list)
    assert len(segment.bounds()) == 4
    assert len(segment.toPoints(0.1)) >= 2

    reversed_ = copy.deepcopy(segment)
    reversed_.reverse()
    assert reversed_.point(0) == pytest.approx(segment.point(1))
    assert reversed_.point(1) == pytest.approx(segment.point(0))

    moved = copy.deepcopy(segment)
    moved.applyTransform(Transform([1, 0, 0, 1, 10, 20]))
    assert moved.point(0.5) == pytest.approx(segment.point(0.5) + (10 + 20j))


#endregion

#region Segment.toPoints


def _distanceToPolyline(p: complex, polyline: list[complex]) -> float:
    """Shortest distance from p to the polyline, measured segment by segment."""
    best = math.inf
    for a, b in zip(polyline, polyline[1:]):
        d = b - a
        if abs(d) < 1e-15:
            best = min(best, abs(p - a))
            continue
        t = max(0.0, min(1.0, ((p - a) * d.conjugate()).real / (d.real ** 2 + d.imag ** 2)))
        best = min(best, abs(p - (a + t * d)))
    return best


def testToPointsKeepsThePointSymmetricSplineCurved():
    """An S-shaped cubic whose inflection sits exactly at t=0.5 puts its
    midpoint exactly on the chord. Checking flatness at the midpoint alone
    would read that as straight and collapse the whole curve to one line, so
    toPoints samples at 1/3 and 2/3 instead."""
    s = CubicBezier(start=0 + 0j, p1=0 + 10j, p2=10 - 10j, end=10 + 0j)
    midpoint = (s.point(0) + s.point(1)) / 2
    assert s.point(0.5) == pytest.approx(midpoint), "fixture is not point-symmetric"

    pts = s.toPoints(0.05)
    assert len(pts) > 2, "the curve collapsed to a straight chord"
    # the curve genuinely bows well off the chord on both sides of the midpoint
    assert max(p.imag for p in pts) > 1.0
    assert min(p.imag for p in pts) < -1.0


def testToPointsRespectsToleranceAndKeepsEndpoints():
    """The returned polyline stays within tolerance of the true curve, measured
    the way it matters: from densely-sampled true points onto the polyline."""
    curve = QuadraticBezier(start=0 + 0j, p1=5 + 10j, end=10 + 0j)
    for tolerance in (0.5, 0.05, 0.005):
        pts = curve.toPoints(tolerance)
        assert pts[0] == pytest.approx(curve.point(0.0))
        assert pts[-1] == pytest.approx(curve.point(1.0))
        worst = max(_distanceToPolyline(curve.point(i / 500), pts) for i in range(501))
        assert worst <= tolerance, f"polyline strays {worst} from the curve at tolerance {tolerance}"

    assert len(curve.toPoints(0.005)) > len(curve.toPoints(0.5)), "a tighter tolerance must sample more finely"


def testLineToPointsIsJustItsEndpoints():
    """Line overrides the recursive subdivision - it is already exact."""
    assert Line(1 + 2j, 9 + 4j).toPoints(1e-9) == [1 + 2j, 9 + 4j]


def testArcToPointsStaysWithinToleranceOfTheCircle():
    arc = Arc(center=0j, u=10 + 0j, v=-10j, t0=0, sweep=math.pi)
    pts = arc.toPoints(0.01)
    # each chord's sagitta is the deviation; check it directly against the circle
    for i in range(len(pts) - 1):
        mid = (pts[i] + pts[i + 1]) / 2
        assert 10 - abs(mid) <= 0.01 + 1e-9


#endregion

#region Line and Arc


def testLineBasics():
    line = Line(1 + 1j, 4 + 5j)
    assert line.length() == pytest.approx(5.0)  # 3-4-5
    assert line.point(0) == pytest.approx(1 + 1j)
    assert line.point(1) == pytest.approx(4 + 5j)
    assert line.point(0.5) == pytest.approx(2.5 + 3j)
    assert line.derivative(0) == pytest.approx(3 + 4j)
    assert line.derivative(1) == pytest.approx(3 + 4j), "a line's derivative is constant"
    assert line.extrema() == []


def testLineReverseAndSubsegment():
    line = Line(0 + 0j, 10 + 0j)
    sub = line.subsegment(0.25, 0.75)
    assert sub.point(0) == pytest.approx(2.5 + 0j)
    assert sub.point(1) == pytest.approx(7.5 + 0j)

    line.reverse()
    assert line.point(0) == pytest.approx(10 + 0j)
    assert line.point(1) == pytest.approx(0 + 0j)


@pytest.mark.parametrize("pt, expected", [
    (0 + 0j, 0.0),
    (5 + 0j, 0.5),
    (10 + 0j, 1.0),
    (-1 + 0j, None),   # before the start
    (11 + 0j, None),   # past the end
])
def testLineTAtPoint(pt, expected):
    t = Line(0 + 0j, 10 + 0j).tAtPoint(pt)
    if expected is None:
        assert t is None
    else:
        assert t == pytest.approx(expected)


def testDegenerateLineTAtPoint():
    """A zero-length line has one point; anything else is off it."""
    degenerate = Line(3 + 3j, 3 + 3j)
    assert degenerate.tAtPoint(3 + 3j) == 0.0
    assert degenerate.tAtPoint(3 + 4j) is None


def testArcBasics():
    """A full circle of radius 5, in the u=(r,0)/v=(0,-r) basis the codebase uses."""
    arc = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi)
    assert arc.length() == pytest.approx(2 * math.pi * 5)
    assert arc.point(0) == pytest.approx(5 + 0j)
    assert arc.point(0.25) == pytest.approx(0 - 5j), "+sweep goes clockwise in this basis"
    assert arc.point(0.5) == pytest.approx(-5 + 0j)
    assert arc.bounds() == pytest.approx((-5, -5, 5, 5))


def testArcReverseSwapsEndpointsAndWinding():
    arc = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=math.pi)
    start, end = arc.point(0), arc.point(1)
    arc.reverse()
    assert arc.point(0) == pytest.approx(end)
    assert arc.point(1) == pytest.approx(start)
    assert arc.sweep == pytest.approx(-math.pi)


def testArcSubsegmentIsExact():
    """Shifting t0 and scaling sweep restricts the arc's own parameterization
    exactly - no re-fitting, so the piece lies on the identical circle."""
    arc = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=math.pi)
    sub = arc.subsegment(0.25, 0.75)
    assert sub.point(0) == pytest.approx(arc.point(0.25))
    assert sub.point(1) == pytest.approx(arc.point(0.75))
    assert sub.point(0.5) == pytest.approx(arc.point(0.5))
    assert abs(sub.u) == pytest.approx(abs(arc.u)), "the radius must not drift"


def testArcFromThreePointsRecoversTheCircle():
    arc = Arc.fromThreePoints(5 + 0j, 0 - 5j, -5 + 0j)
    assert arc is not None
    assert arc.center == pytest.approx(0j)
    assert abs(arc.u) == pytest.approx(5.0)
    assert arc.point(0) == pytest.approx(5 + 0j)
    assert arc.point(1) == pytest.approx(-5 + 0j)
    assert arc.point(0.5) == pytest.approx(0 - 5j), "the fit must sweep through the middle point"


def testArcFromThreePointsSweepsThroughTheMiddlePointNotTheLongWay():
    """The winding is chosen by which candidate actually passes through pm."""
    upper = Arc.fromThreePoints(5 + 0j, 0 + 5j, -5 + 0j)
    assert upper is not None
    assert upper.point(0.5) == pytest.approx(0 + 5j)
    assert upper.sweep < 0, "sweeping through +y is counter-clockwise in this basis"


@pytest.mark.parametrize("p0, pm, p1", [
    (0 + 0j, 1 + 0j, 2 + 0j),      # exactly collinear
    (0 + 0j, 1 + 1e-12j, 2 + 0j),  # collinear to within noise
    (0 + 0j, 0 + 0j, 2 + 0j),      # coincident points
])
def testArcFromThreePointsRejectsCollinearInput(p0, pm, p1):
    """No circle passes through collinear points, and near-collinear input makes
    the circumcircle numerically unstable."""
    assert Arc.fromThreePoints(p0, pm, p1) is None


def testMaxRadiusToChordRejectsAnUnstableFit():
    """A barely-bowed arc has a huge radius relative to its chord - the regime
    where tiny input noise swings the computed centre wildly."""
    barelyBowed = (0 + 0j, 1 + 0.0001j, 2 + 0j)
    assert Arc.fromThreePoints(*barelyBowed) is not None, "the fixture must be fittable without the guard"
    assert Arc.fromThreePoints(*barelyBowed, maxRadiusToChord=20.0) is None


@pytest.mark.parametrize("pt, inside", [
    (5 + 0j, True),     # t = 0
    (0 - 5j, True),     # t = 0.5, mid-sweep
    (-5 + 0j, True),    # t = 1
    (0 + 5j, False),    # on the circle, but outside the swept range
])
def testArcTAtPointOnlyAcceptsTheSweptRange(pt, inside):
    arc = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=math.pi)
    assert (arc.tAtPoint(pt) is not None) is inside


def testDegenerateArcBasisYieldsNoParameter():
    assert Arc(center=0j, u=0j, v=0j, t0=0, sweep=math.pi).tAtPoint(1 + 1j) is None


#endregion

#region intersections


def testLineIntersectsLine():
    assert (Line(0 + 0j, 10 + 0j) @ Line(5 - 5j, 5 + 5j)) == [5 + 0j]


@pytest.mark.parametrize("other, why", [
    (Line(0 + 1j, 10 + 1j), "parallel"),
    (Line(0 + 0j, 10 + 0j), "collinear overlap has no single point"),
    (Line(20 - 5j, 20 + 5j), "crossing point is outside both segments"),
])
def testLinesWithNoSingleIntersection(other, why):
    assert (Line(0 + 0j, 10 + 0j) @ other) == [], why


def testLineIntersectsArcAndDispatchesBothWays():
    """Arc declines a Line operand so Python re-dispatches to Line.__rmatmul__ -
    both orders must give the same (unordered) answer."""
    line = Line(0 + 0j, 10 + 0j)
    circle = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi)
    # (-5,0) is on the circle but off the segment, so only one hit survives
    assert (line @ circle) == [5 + 0j]
    assert (circle @ line) == (line @ circle)


def testLineMissingTheCircleEntirely():
    circle = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi)
    assert (Line(0 + 20j, 10 + 20j) @ circle) == []


def testTangentLineCollapsesTheDoubleRoot():
    """A tangent is a repeated root - it must report one point, not two."""
    circle = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi)
    hits = Line(-10 + 5j, 10 + 5j) @ circle
    assert len(hits) == 1
    assert hits[0] == pytest.approx(0 + 5j, abs=1e-6)


def testArcIntersectsArc():
    a = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi)
    b = Arc(center=8 + 0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi)
    hits = sorted(a @ b, key=lambda p: p.imag)
    assert len(hits) == 2
    assert hits[0] == pytest.approx(4 - 3j)
    assert hits[1] == pytest.approx(4 + 3j)


@pytest.mark.parametrize("centre, radius, why", [
    (100 + 0j, 5, "too far apart"),
    (0 + 0j, 2, "concentric - nested, no crossing"),
    (0 + 0j, 5, "coincident - infinitely many points, so no single ones"),
])
def testArcsWithNoIntersectionPoints(centre, radius, why):
    a = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi)
    b = Arc(center=centre, u=radius + 0j, v=-radius * 1j, t0=0, sweep=2 * math.pi)
    assert (a @ b) == [], why


def testEllipticalArcArcIntersectionIsRejected():
    """The two-circle construction doesn't generalise to ellipses, and silently
    returning a wrong point would be worse than refusing."""
    circle = Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi)
    ellipse = Arc(center=4 + 0j, u=8 + 0j, v=-3j, t0=0, sweep=2 * math.pi)
    with pytest.raises(NotImplementedError):
        _ = circle @ ellipse


def testSubsegmentIsUndefinedForBeziers():
    """No general formula exists without re-deriving control points, and every
    caller only ever runs on already-tessellated Line/Arc geometry."""
    with pytest.raises(NotImplementedError):
        CubicBezier(0j, 1 + 1j, 2 + 1j, 3 + 0j).subsegment(0.2, 0.8)
    with pytest.raises(NotImplementedError):
        QuadraticBezier(0j, 1 + 1j, 2 + 0j).subsegment(0.2, 0.8)


#endregion

#region beziers


def testQuadraticBezierBasics():
    curve = QuadraticBezier(start=0 + 0j, p1=5 + 10j, end=10 + 0j)
    assert curve.point(0) == pytest.approx(0 + 0j)
    assert curve.point(1) == pytest.approx(10 + 0j)
    assert curve.point(0.5) == pytest.approx(5 + 5j), "the peak sits halfway to the control point"
    assert curve.derivative(0) == pytest.approx(2 * (curve.p1 - curve.start))
    assert curve.derivative(1) == pytest.approx(2 * (curve.end - curve.p1))


def testCubicBezierBasics():
    curve = CubicBezier(start=0 + 0j, p1=0 + 10j, p2=10 + 10j, end=10 + 0j)
    assert curve.point(0) == pytest.approx(0 + 0j)
    assert curve.point(1) == pytest.approx(10 + 0j)
    assert curve.point(0.5) == pytest.approx(5 + 7.5j)
    assert curve.derivative(0) == pytest.approx(3 * (curve.p1 - curve.start))
    assert curve.derivative(1) == pytest.approx(3 * (curve.end - curve.p2))


@pytest.mark.parametrize("curve", [
    QuadraticBezier(start=0 + 0j, p1=2 + 8j, end=10 + 0j),
    CubicBezier(start=0 + 0j, p1=1 + 9j, p2=8 + 4j, end=10 + 0j),
], ids=["quadratic", "cubic"])
def testBezierReverseKeepsTheSameCurve(curve):
    """Reversal must re-order the interior control points too - swapping only
    the endpoints leaves a differently-shaped curve."""
    before = [curve.point(i / 10) for i in range(11)]
    curve.reverse()
    after = [curve.point(1 - i / 10) for i in range(11)]
    for b, a in zip(before, after):
        assert a == pytest.approx(b)


def testBezierLengthMatchesAStraightControlPolygon():
    """A bezier whose control points are collinear and evenly spaced is just a
    straight line, so its arc length is exactly the chord."""
    assert CubicBezier(0 + 0j, 3 + 0j, 6 + 0j, 9 + 0j).length() == pytest.approx(9.0)
    assert QuadraticBezier(0 + 0j, 5 + 0j, 10 + 0j).length() == pytest.approx(10.0)


#endregion

#region bounds


@pytest.mark.parametrize("segment, expected", [
    (Line(1 + 2j, 4 + 6j), (1, 2, 4, 6)),
    (Line(4 + 6j, 1 + 2j), (1, 2, 4, 6)),                                  # direction-independent
    (Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi), (-5, -5, 5, 5)),
    # a quarter arc's box is its endpoints only - no extremum falls inside the sweep
    (Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=math.pi / 2), (0, -5, 5, 0)),
    # the bezier bows to y=5 at t=0.5, well short of its control point at y=10
    (QuadraticBezier(0 + 0j, 5 + 10j, 10 + 0j), (0, 0, 10, 5)),
    # a symmetric arch: its y-derivative is linear, not quadratic, so solving
    # only the quadratic case would find no extremum and miss the whole arch
    (CubicBezier(0 + 0j, 0 + 10j, 10 + 10j, 10 + 0j), (0, 0, 10, 7.5)),
])
def testSegmentBounds(segment, expected):
    """Bounds follow the true curve, so a bezier's box is tighter than its
    control polygon's."""
    assert segment.bounds() == pytest.approx(expected)


def testBoundsAreClampedToTheRenderersRange():
    """Bambu Studio's preview breaks on very large coordinates, so bounds clamp
    to [-5000, 5256] rather than reporting the true extent."""
    assert Line(-99999 + 0j, 99999 + 0j).bounds() == pytest.approx((-5000, 0, 5256, 0))


def testPathAndObjectAndDocumentBoundsAggregate():
    """Each level takes the union of the level below."""
    left = Path.fromPoints([0 + 0j, 5 + 5j], closed=False)
    right = Path.fromPoints([-3 - 3j, 1 + 1j], closed=False)

    assert left.bounds() == pytest.approx((0, 0, 5, 5))

    obj = PathObject("multi", [left, right])
    assert obj.bounds() == pytest.approx((-3, -3, 5, 5))

    document = Document()
    document.add(obj)
    document.add(PathObject("far", [Path.fromPoints([20 + 20j, 30 + 30j], closed=False)]))
    assert document.bounds() == pytest.approx((-3, -3, 30, 30))


#endregion

#region Path


def testPathLengthAndEndpoints():
    square = Path.fromPoints(SQUARE_PTS, closed=True)
    assert square.length() == pytest.approx(40.0)
    assert square.start() == pytest.approx(0 + 0j)
    assert square.end() == pytest.approx(0 + 0j)


def testPathPointSpansTheWholeSubpath():
    """t is normalized across every segment, not per-segment - so t=0.25 on a
    4-segment square lands exactly on the first corner."""
    square = Path.fromPoints(SQUARE_PTS, closed=True)
    assert square.point(0.0) == pytest.approx(0 + 0j)
    assert square.point(0.25) == pytest.approx(10 + 0j)
    assert square.point(0.5) == pytest.approx(10 + 10j)
    assert square.point(0.75) == pytest.approx(0 + 10j)
    assert square.point(1.0) == pytest.approx(0 + 0j)
    assert square.point(0.125) == pytest.approx(5 + 0j), "halfway along the first edge"


def testPathPointClampsOutOfRangeParameters():
    square = Path.fromPoints(SQUARE_PTS, closed=True)
    assert square.point(-1.0) == pytest.approx(square.point(0.0))
    assert square.point(2.0) == pytest.approx(square.point(1.0))


def testIsClosedSeparatesFromIsFillable():
    """The two are independent: an open path can enclose area, and a closed one
    can enclose none."""
    openSquare = Path.fromPoints(SQUARE_PTS, closed=False)
    assert not openSquare.isClosed()
    assert openSquare.isFillable(), "an open path still encloses area"

    outAndBack = Path.fromPoints([0 + 0j, 10 + 0j], closed=True)
    assert outAndBack.isClosed()
    assert not outAndBack.isFillable(), "a degenerate out-and-back encloses nothing"


def testSelfIntersectingLobesDoNotCancel():
    """A bowtie's two lobes wind opposite ways. Summing signed shoelace terms
    would cancel them to zero and read as unfillable, so the absolute value is
    taken per-term instead."""
    bowtie = Path.fromPoints([0 + 0j, 10 + 10j, 10 + 0j, 0 + 10j], closed=True)
    assert bowtie.isFillable()


def testVerticesOmitTheDuplicateClosingPoint():
    """pyclipper's AddPath expects a closed ring without the repeated start, so
    a closed path reports each corner once - while an open one still needs its
    final endpoint, which is no segment's start."""
    assert Path.fromPoints(SQUARE_PTS, closed=True).vertices() == SQUARE_PTS
    assert Path.fromPoints(SQUARE_PTS, closed=False).vertices() == SQUARE_PTS

    # the open path's last vertex comes from end(), not from a 4th segment
    assert len(Path.fromPoints(SQUARE_PTS, closed=False).segments) == 3


def testRotateToReanchorsAClosedPath():
    square = Path.fromPoints(SQUARE_PTS, closed=True)
    square.rotateTo(2)
    assert square.start() == pytest.approx(10 + 10j)
    assert square.isClosed(), "re-anchoring must leave the loop closed"
    assert square.length() == pytest.approx(40.0), "and must not change what is drawn"


def testRotateToRejectsOpenPaths():
    with pytest.raises(ValueError):
        Path.fromPoints(SQUARE_PTS, closed=False).rotateTo(1)


def testPathReverseFlipsOrderAndEachSegment():
    path = Path.fromPoints([0 + 0j, 10 + 0j, 10 + 10j], closed=False)
    path.reverse()
    assert path.start() == pytest.approx(10 + 10j)
    assert path.end() == pytest.approx(0 + 0j)
    # the segments must be individually reversed too, or the path would jump
    for i in range(len(path.segments) - 1):
        assert path.segments[i].point(1) == pytest.approx(path.segments[i + 1].point(0))


def testFromPointsClosedAddsTheReturnSegment():
    assert len(Path.fromPoints(SQUARE_PTS, closed=False).segments) == 3
    assert len(Path.fromPoints(SQUARE_PTS, closed=True).segments) == 4


def testTessellateCarriesTheLineTypeThrough():
    """The draw role has to survive tessellation, or generated geometry would
    lose track of which height/speed/feature label it belongs to."""
    path = Path.fromPoints(SQUARE_PTS, closed=True)
    path.lineType = LineType.GAP_INFILL
    assert path.tessellate(0.01).lineType is LineType.GAP_INFILL
    assert path.tessellate(0.01, allowArcs=False).lineType is LineType.GAP_INFILL


def testTessellateIsNonMutating():
    path = Path.fromPoints(SQUARE_PTS, closed=True)
    before = list(path.segments)
    path.tessellate(0.01)
    assert path.segments == before


def testTessellateOfAnEmptyPathIsEmpty():
    assert Path([], lineType=LineType.INFILL).tessellate(0.01).segments == []


def testTessellateWithoutArcsEmitsOnlyLines():
    circle = Path([Arc(center=0j, u=5 + 0j, v=-5j, t0=0, sweep=2 * math.pi)])
    flat = circle.tessellate(0.05, allowArcs=False)
    assert flat.segments, "the circle vanished"
    assert all(isinstance(s, Line) for s in flat.segments)


#endregion

#region PathObject


def testApplyTransformationsMovesEverySegmentType():
    """Every Segment subclass has to implement applyTransform - a missed control
    point would warp the curve rather than move it."""
    transform = Transform()
    transform.scale(2)
    obj = PathObject("o", [Path([
        Line(0 + 0j, 1 + 0j),
        Arc(center=0j, u=1 + 0j, v=-1j, t0=0, sweep=math.pi),
        QuadraticBezier(0 + 0j, 1 + 1j, 2 + 0j),
        CubicBezier(0 + 0j, 1 + 1j, 2 + 1j, 3 + 0j),
    ])], Style(), transform)
    obj.applyTransformations()

    line, arc, quad, cubic = obj.geometry[0].segments
    # transforming must not change what each segment *is*, and narrows the types
    # so the control points below can be read directly
    assert isinstance(line, Line)
    assert isinstance(arc, Arc)
    assert isinstance(quad, QuadraticBezier)
    assert isinstance(cubic, CubicBezier)

    assert line.start == pytest.approx(0 + 0j)
    assert line.end == pytest.approx(2 + 0j)
    assert arc.u == pytest.approx(2 + 0j)
    assert arc.v == pytest.approx(-2j)
    assert quad.p1 == pytest.approx(2 + 2j)
    assert quad.end == pytest.approx(4 + 0j)
    assert cubic.p1 == pytest.approx(2 + 2j)
    assert cubic.p2 == pytest.approx(4 + 2j)
    assert cubic.end == pytest.approx(6 + 0j)


def testApplyTransformationsAppliesToEverySubpath():
    transform = Transform()
    transform.translate(10, 20)
    obj = PathObject("o", [
        Path.fromPoints([0 + 0j, 1 + 0j], closed=False),
        Path.fromPoints([5 + 5j, 6 + 5j], closed=False),
    ], Style(), transform)
    obj.applyTransformations()
    assert obj.geometry[0].start() == pytest.approx(10 + 20j)
    assert obj.geometry[1].start() == pytest.approx(15 + 25j)


def testApplyTransformationsScalesStrokeAndDashLengths():
    """strokeWidth, dasharray and dashoffset share the geometry's length space,
    so all three scale with it - otherwise a scaled shape's dashes drift out of
    step with its own stroke width."""
    transform = Transform()
    transform.scale(2)
    obj = PathObject("o", [Path([Line(0j, 1 + 0j)])],
                     Style(strokeWidth=2.0, dasharray=[4.0, 1.0], dashoffset=3.0), transform)
    obj.applyTransformations()
    assert obj.style.strokeWidth == pytest.approx(4.0)
    assert obj.style.dasharray == pytest.approx([8.0, 2.0])
    assert obj.style.dashoffset == pytest.approx(6.0)


def testNonUniformScaleUsesTheUniformEquivalent():
    """A single width can't express a non-uniform stroke scale, so sqrt(|det|)
    stands in for it."""
    transform = Transform()
    transform.scale(4, 1)
    obj = PathObject("o", [Path([Line(0j, 1 + 0j)])], Style(strokeWidth=1.0), transform)
    obj.applyTransformations()
    assert obj.style.strokeWidth == pytest.approx(2.0)


def testMirrorScalesStrokeWidthPositively():
    """|det| is taken before the root, so a negative-determinant mirror doesn't
    produce a negative (or complex) stroke width."""
    transform = Transform()
    transform.scale(-3, 3)
    obj = PathObject("o", [Path([Line(0j, 1 + 0j)])], Style(strokeWidth=1.0), transform)
    obj.applyTransformations()
    assert obj.style.strokeWidth == pytest.approx(3.0)


def testSolidStrokeStaysSolidThroughATransform():
    obj = PathObject("o", [Path([Line(0j, 1 + 0j)])],
                     Style(strokeWidth=1.0, dasharray=None, dashoffset=5.0),
                     Transform([2, 0, 0, 2, 0, 0]))
    obj.applyTransformations()
    assert obj.style.dasharray is None
    assert obj.style.dashoffset == 5.0, "an unused dashoffset must not be scaled"


def testApplyTransformationsResetsTheTransform():
    """The transform is baked into the geometry, so re-applying must be a no-op."""
    transform = Transform()
    transform.scale(2)
    obj = PathObject("o", [Path([Line(0j, 1 + 0j)])], Style(strokeWidth=2.0), transform)
    obj.applyTransformations()
    assert obj.transform.matrix == [1, 0, 0, 1, 0, 0]

    obj.applyTransformations()
    assert obj.geometry[0].end() == pytest.approx(2 + 0j), "geometry moved a second time"
    assert obj.style.strokeWidth == pytest.approx(4.0), "stroke width scaled a second time"


def testPathObjectIsClosedNeedsASingleClosedSubpath():
    closed = Path.fromPoints(SQUARE_PTS, closed=True)
    assert PathObject("one", [closed]).isClosed()
    assert not PathObject("open", [Path.fromPoints(SQUARE_PTS, closed=False)]).isClosed()
    assert not PathObject("two", [closed, copy.deepcopy(closed)]).isClosed(), "two loops are not 'a' closed loop"


@pytest.mark.parametrize("method, args", [("vertices", ()), ("rotateTo", (1,))])
def testClosedOnlyHelpersRejectOtherObjects(method, args):
    obj = PathObject("open", [Path.fromPoints(SQUARE_PTS, closed=False)])
    with pytest.raises(ValueError):
        getattr(obj, method)(*args)


def testPathObjectReverseFlipsSubpathOrderAndDirection():
    obj = PathObject("o", [
        Path.fromPoints([0 + 0j, 1 + 0j], closed=False),
        Path.fromPoints([5 + 0j, 6 + 0j], closed=False),
    ])
    obj.reverse()
    assert obj.start() == pytest.approx(6 + 0j), "was the last subpath's end"
    assert obj.end() == pytest.approx(0 + 0j), "was the first subpath's start"


def testIaddAppendsToTheLastSubpath():
    obj = PathObject("o", [Path([Line(0j, 1 + 0j)]), Path([Line(5 + 0j, 6 + 0j)])])
    obj += Line(6 + 0j, 7 + 0j)
    assert len(obj.geometry[0].segments) == 1
    assert len(obj.geometry[1].segments) == 2
    assert obj.end() == pytest.approx(7 + 0j)


def testPathObjectPointDelegatesToTheNamedSubpath():
    obj = PathObject("o", [
        Path.fromPoints([0 + 0j, 10 + 0j], closed=False),
        Path.fromPoints([0 + 5j, 10 + 5j], closed=False),
    ])
    assert obj.point(0, 0.5) == pytest.approx(5 + 0j)
    assert obj.point(1, 0.5) == pytest.approx(5 + 5j)


#endregion

#region Document


def testDocumentAddTracksOrderAndIdLookup():
    document = Document()
    first = PathObject("a", [Path.fromPoints([0 + 0j, 1 + 0j], closed=False)])
    second = PathObject("b", [Path.fromPoints([2 + 0j, 3 + 0j], closed=False)])
    document.add(first)
    document.add(second)

    assert document.objects == [first, second], "insertion order is the draw order"
    assert document.id["a"] is first
    assert document.id["b"] is second


def testDuplicateIdKeepsBothObjectsButOnlyTheLastLookup():
    """The known id-collision case: both objects still draw, but the id map can
    only point at one of them - which is why lib/stroke.py's dropRawGeometry
    checks identity before deleting an entry."""
    document = Document()
    first = PathObject("dup", [Path.fromPoints([0 + 0j, 1 + 0j], closed=False)])
    second = PathObject("dup", [Path.fromPoints([2 + 0j, 3 + 0j], closed=False)])
    document.add(first)
    document.add(second)

    assert len(document.objects) == 2
    assert document.id["dup"] is second


def testEmptyDocumentBoundsAreInverted():
    """No objects means no extent - the seed values come back untouched, which
    callers can detect as min > max."""
    xmin, ymin, xmax, ymax = Document().bounds()
    assert xmin == math.inf and ymin == math.inf
    assert xmax == -math.inf and ymax == -math.inf


#endregion
