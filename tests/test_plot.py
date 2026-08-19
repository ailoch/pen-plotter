"""Unit tests for lib/plot.py's gcode-emitting helpers.

plot.py is the end of the pipeline: whatever it gets wrong lands in the file a
printer actually executes, where the only feedback is a bad drawing. These tests
drive the helpers directly against an in-memory file and assert on the exact
lines emitted, rather than diffing a whole output file - speeds, spacing and
routing all legitimately change that.
"""
import cmath
import glob
import io
import math
import pathlib

import pytest

from lib.geometry import Arc, Document, Line, Path, PathObject
from lib.plot import (
    _LOAD_PROGRESS_MAX_INTERVAL, _LOAD_PROGRESS_MAX_STEP, _LOAD_PROGRESS_MIN_STEP,
    _DrawState, _addLine, _addPath, _bedExcludeArea, _bridgeContours, _canvasBoundsNozzle,
    _emitSegment, _evalTemplateBlock, _eValue, _fmtNum, _inBounds, _moveRect,
    _nextSegmentType, _penMove, _perimeterWalk, _printableArea, _removeRedundantPoints,
    _renderTemplate, _skipRepeatedClosingColor, _splitAtBounds, _waitForPen, createFile,
)
from lib.settings import LineType, Settings

_DRAW_Z = 2.0
_TRAVEL_Z = 3.5

# the dataclass defaults leave speeds/accels/lineTypes empty and every draw height at
# 0, which _addLine elides as a falsy arg - almost nothing would reach the file. speeds
# and accels stay empty here on purpose so the assertions below aren't buried in F/M204
# lines; the tests that care about those pass their own.
def _settings(**overrides) -> Settings:
    base = dict(
        heights={LineType.STROKE: _DRAW_Z, LineType.INFILL: _DRAW_Z, LineType.GAP_INFILL: _DRAW_Z,
                 LineType.INVALID: _DRAW_Z, LineType.TRAVEL: _TRAVEL_Z},
        speeds={},
        accels={},
        shortTravelThresholds={LineType.STROKE: .5, LineType.INFILL: .5, LineType.GAP_INFILL: .5,
                               LineType.INVALID: .5},
        lineTypes={LineType.STROKE: "Stroke", LineType.INFILL: "Infill",
                   LineType.GAP_INFILL: "Gap", LineType.INVALID: "Invalid"},
        styleChangeMessage="; FEATURE: %s",
        canvasSize=100 + 100j,
        canvasOffset=0j,
        penOffset=0j,
    )
    return Settings(**{**base, **overrides})

def _state(x: float = 0, y: float = 0, z: float = _TRAVEL_Z) -> _DrawState:
    return _DrawState(pos={"X": x, "Y": y, "Z": z})

# runs one emitter against a fresh in-memory file and returns the gcode lines
def _emit(fn, state: _DrawState | None = None) -> list[str]:
    file = io.StringIO()
    fn(state if state is not None else _state(), file)
    return file.getvalue().splitlines()


#region number formatting


@pytest.mark.parametrize("value, expected", [
    (256.0, "256"),         # a whole number loses its ".0"
    (100.0, "100"),         # ...without the integer part's own zeros being stripped too
    (12.5, "12.5"),
    (0, "0"),               # trimming must not strip the number away entirely
    (-3.0, "-3"),
    (12.3456789, "12.34568"),
    (1e-7, "0"),            # below the 5dp the printer is given, so it rounds away
])
def testFmtNumTrimsTrailingZeros(value, expected):
    assert _fmtNum(value) == expected


#endregion

#region bounding-box rect (_moveRect)


def testMoveRectTracesTheRectAndReturnsToTheStart():
    """showBoundingBoxes debug output: a travel to the first corner, then four draw
    moves back around to it."""
    lines = _emit(lambda st, f: _moveRect(st, _settings(), (10.0, 20.0, 40.0, 60.0), f, LineType.STROKE))
    assert lines == [
        "G1 X10 Y20",   # travel in
        "G1 Z2",        # ...and down to draw height
        "; FEATURE: Stroke",
        "G1 Y60 E40",
        "G1 X40 E30",
        "G1 Y20 E40",
        "G1 X10 E30",
    ]


#endregion

#region template expressions


_TEMPLATE_NS: dict[str, str | float] = {"SPEED": 3000.0, "HEIGHT": 10.0, "NAME": "0x2"}

@pytest.mark.parametrize("expr, expected", [
    ("SPEED", "3000"),          # a bare name, the original behaviour
    ("NAME", "0x2"),            # a string value passes straight through
    ("SPEED/2", "1500"),
    ("HEIGHT + 10", "20"),
    ("-HEIGHT", "-10"),         # unary minus
    ("+HEIGHT", "10"),          # unary plus
    ("SPEED//7", "428"),
    ("SPEED%7", "4"),
    ("2*3", "6"),               # numeric literals need no namespace at all
])
def testTemplateBlocksEvaluateArithmetic(expr, expected):
    assert _evalTemplateBlock(expr, _TEMPLATE_NS) == expected

@pytest.mark.parametrize("expr", [
    "().__class__",                                # the traversal that escapes a bare eval
    "().__class__.__bases__[0].__subclasses__()",  # ...all the way to arbitrary code
    "len(NAME)",                                   # any call at all
    "NAME[0]",                                     # subscript
    "2**999999999",                                # resource exhaustion via **
    "'x'*999999999",                               # ...and via a string literal
    "True",                                        # bools are excluded from "numeric"
    "NAME*2",                                      # arithmetic on a string value
    "NOPE",                                        # name not in the namespace
    "SPEED +",                                     # not even valid python
])
def testDisallowedTemplateExpressionsAreLeftVerbatim(expr, capsys):
    """A prefix/suffix template can arrive alongside a shared config, so a block that
    can't be evaluated under the whitelist must be inert - left in place with a
    warning, never executed."""
    assert _evalTemplateBlock(expr, _TEMPLATE_NS) == "{" + expr + "}"
    assert "Warning" in capsys.readouterr().out

def testRenderTemplateSubstitutesEveryBlockAndCopiesPlainLines(tmp_path):
    src = tmp_path / "template.gcode"
    src.write_text("G1 F{SPEED}\n; no braces here\nX{SPEED/2} Y{NAME}\n")
    assert _renderTemplate(str(src), _TEMPLATE_NS) == "G1 F3000\n; no braces here\nX1500 Y0x2\n"

def testTemplateWarningNamesItsSourceFile(capsys):
    """createFile passes its own prefix/suffix path through as sourceName, so a bad
    block in one names *which* template file it came from."""
    _evalTemplateBlock("NOPE", _TEMPLATE_NS, "gcode_templates/my_prefix.gcode")
    assert "gcode_templates/my_prefix.gcode" in capsys.readouterr().out

def testTemplateWarningOmitsLocationWhenNoneIsGiven(capsys):
    _evalTemplateBlock("NOPE", _TEMPLATE_NS)
    assert " in ''" not in capsys.readouterr().out


#endregion

#region segment color cycling


_SEGMENT_TYPES = ("a", "b", "c")

@pytest.mark.parametrize("last, expected", [
    ("", "a"),          # nothing drawn yet
    ("a", "b"),
    ("c", "a"),         # wraps
    ("zzz", "a"),       # an unrecognised name restarts the cycle
])
def testNextSegmentTypeAdvancesCyclically(last, expected):
    assert _nextSegmentType(last, _SEGMENT_TYPES) == expected

def testSkipRepeatedClosingColorOnlyFiresOnACollision():
    settings = _settings(segmentTypes=_SEGMENT_TYPES)

    state = _state()
    state.lastMoveType = "c"                        # next would be "a"
    _skipRepeatedClosingColor(state, settings, "a")
    assert state.lastMoveType == "a", "the synthetic tick lands the real draw on b instead"

    state.lastMoveType = "a"                        # next would be "b", no collision
    _skipRepeatedClosingColor(state, settings, "a")
    assert state.lastMoveType == "a"


#endregion

#region gcode line assembly


def testAddLineElidesFalsyArgs():
    """Every falsy arg is dropped, which is what makes _eValue's 0 disappear."""
    assert _emit(lambda st, f: _addLine(st, _settings(), {"G": "1", "X": 5, "Y": 5, "E": 0}, f)) == ["G1 X5 Y5"]

def testAddLineSkipsAxesAlreadyAtTheTargetValue():
    state = _state(x=5)
    assert _emit(lambda st, f: _addLine(st, _settings(), {"G": "1", "X": 5, "Y": 7}, f), state) == ["G1 Y7"]
    assert state.pos["Y"] == 7

def testAddLineWritesNothingWhenNoAxisMoves():
    """A line needs an X/Y/Z to be worth writing - otherwise it's a bare G1."""
    assert _emit(lambda st, f: _addLine(st, _settings(), {"G": "1", "X": 0, "Y": 0}, f)) == []

def testAMoveToExactlyZeroIsNotElidedAsFalsy():
    """0 is a real axis value, not a stand-in for "not provided" - only None (a param
    left out of settings, e.g. an unconfigured speed) means that."""
    state = _state(z=3.5)
    assert _emit(lambda st, f: _addLine(st, _settings(), {"G": "1", "Z": 0}, f), state) == ["G1 Z0"]
    assert state.pos["Z"] == 0

def testAnUnconfiguredSpeedOrAccelIsSkippedNotWrittenAsZero():
    """F/accel come from settings.speeds.get(lineType)/.accels.get(lineType), which is
    None when that role has no entry - that must stay silent, not turn into F0/M204 S0."""
    settings = _settings(speeds={}, accels={}, styleChangeMessage="")
    assert _emit(lambda st, f: _addLine(st, settings, {"G": "1", "X": 5, "E": 5}, f, LineType.STROKE)) == ["G1 X5 E5"]

def testArcsAreExemptFromTheAxisRequirement():
    """A full circle ends where it started, so G2/G3 must survive with no axis change."""
    assert _emit(lambda st, f: _addLine(st, _settings(), {"G": "2", "X": 0, "Y": 0, "I": 1, "J": 1}, f)) == ["G2 I1 J1"]

def testSpeedAndAccelAreOnlyEmittedWhenTheyChange():
    settings = _settings(speeds={LineType.STROKE: 600}, accels={LineType.STROKE: 500})
    state = _state()
    file = io.StringIO()
    _addLine(state, settings, {"G": "1", "X": 5, "E": 5}, file, LineType.STROKE)
    _addLine(state, settings, {"G": "1", "X": 9, "E": 4}, file, LineType.STROKE)
    assert file.getvalue().splitlines() == [
        "M204 S500",                # accel goes out on its own line, ahead of the move
        "; FEATURE: Stroke",
        "G1 X5 E5 F600",
        "G1 X9 E4",                 # same role -> neither the accel nor the feed repeats
    ]


#endregion

#region E axis scaling


@pytest.mark.parametrize("multiplier,expected", [(1, 10), (0.5, 5), (2, 20)])
def testEValueScalesByTheMultiplier(multiplier, expected):
    assert _eValue(Settings(eAxisMultiplier=multiplier), 10) == expected

@pytest.mark.parametrize("multiplier", [0, -0.5, -3])
def testNonPositiveMultiplierDropsE(multiplier):
    """settings._validate warns that a multiplier <= 0 drops E from every draw move,
    so every path to that must actually produce a droppable 0. A negative one is the
    case that bites: _addLine elides falsy args, and a negative E is truthy, so it
    would emit a *retraction* on every draw move rather than nothing at all."""
    assert _eValue(Settings(eAxisMultiplier=multiplier), 10) == 0


#endregion

#region pen moves


def testLongTravelLiftsMovesThenLowers():
    lines = _emit(lambda st, f: _penMove(st, _settings(), 50 + 0j, f, True, LineType.STROKE), _state(z=_DRAW_Z))
    assert lines == ["G1 Z3.5", "G1 X50", "G1 Z2"]

def testShortTravelStaysPenDown():
    """Under shortTravelThresholds the pen never lifts - just an XY move at the
    arriving role's own settings."""
    lines = _emit(lambda st, f: _penMove(st, _settings(), 0.2 + 0j, f, True, LineType.STROKE), _state(z=_DRAW_Z))
    assert lines == ["G1 X0.2"]

@pytest.mark.parametrize("departing, arriving", [
    (LineType.STROKE, LineType.INFILL),     # the arriving role is the strict one
    (LineType.INFILL, LineType.STROKE),     # ...and the departing one, which only the min catches
])
def testCrossRoleTravelUsesTheStricterThreshold(departing, arriving):
    """A 1mm hop is short for STROKE but long for INFILL, so a travel between the two
    must lift either way round - taking the arriving role's threshold alone would let
    the hop hide behind whichever role happens to be laxer."""
    settings = _settings(shortTravelThresholds={LineType.STROKE: 5.0, LineType.INFILL: 0.1,
                                                LineType.GAP_INFILL: .5, LineType.INVALID: .5})
    state = _state(z=_DRAW_Z)
    state.lastLineType = departing
    assert _emit(lambda st, f: _penMove(st, settings, 1 + 0j, f, True, arriving), state) == \
        ["G1 Z3.5", "G1 X1", "G1 Z2"]

def testSameRoleTravelOnlyUsesItsOwnThreshold():
    """The min is over the two roles involved, so a same-role hop well under that
    role's own threshold still stays down."""
    settings = _settings(shortTravelThresholds={LineType.STROKE: 5.0, LineType.INFILL: 0.1,
                                                LineType.GAP_INFILL: .5, LineType.INVALID: .5})
    state = _state(z=_DRAW_Z)
    state.lastLineType = LineType.STROKE
    assert _emit(lambda st, f: _penMove(st, settings, 1 + 0j, f, True, LineType.STROKE), state) == ["G1 X1"]

def testDrawMoveLowersThenExtrudesTheSegmentLength():
    lines = _emit(lambda st, f: _penMove(st, _settings(), 3 + 4j, f, False, LineType.STROKE))
    assert lines == ["G1 Z2", "; FEATURE: Stroke", "G1 X3 Y4 E5"], "E is the 3-4-5 length"

def testRaisedDrawsSitAThousandthHigher():
    """The cosmetic per-object Z offset that keeps the slicer preview's layers apart."""
    lines = _emit(lambda st, f: _penMove(st, _settings(), 3 + 4j, f, False, LineType.STROKE, raised=True))
    assert lines[0] == "G1 Z2.001"

def testSubMicronMovesAreDroppedAsRoundingNoise():
    assert _emit(lambda st, f: _penMove(st, _settings(), 0.0001 + 0j, f, False, LineType.STROKE)) == []

@pytest.mark.parametrize("sweep, code", [(math.pi, "G2"), (-math.pi, "G3")])
def testEmitSegmentPicksArcDirectionFromSweep(sweep, code):
    arc = Arc(center=10 + 0j, u=-10 + 0j, v=0 - 10j, t0=0, sweep=sweep)
    lines = _emit(lambda st, f: _emitSegment(st, _settings(), arc, f, LineType.STROKE, False))
    assert [l.split()[0] for l in lines if l.startswith("G")] == ["G1", code], "G1 is the Z drop"

def testEmitSegmentSetsArcHeightSeparately():
    """G2/G3 carries only the endpoint, so unlike a Line draw it has no move of its own
    to piggyback the height change on."""
    arc = Arc(center=10 + 0j, u=-10 + 0j, v=0 - 10j, t0=0, sweep=math.pi)
    assert "G1 Z2" in _emit(lambda st, f: _emitSegment(st, _settings(), arc, f, LineType.STROKE, False))


#endregion

#region canvas bounds


def testCanvasBoundsAreShiftedIntoNozzleSpace():
    """Segment coordinates are always nozzle space, so the canvas rect has to be too -
    showPenPos only relabels the preview, it doesn't move anything."""
    settings = _settings(canvasOffset=20 + 30j, canvasSize=100 + 100j, penOffset=10 + 5j)
    assert _canvasBoundsNozzle(settings) == (10.0, 25.0, 110.0, 125.0)

@pytest.mark.parametrize("pt, expected", [
    (50 + 50j, True),
    (0 + 0j, True),         # corners count as inside
    (100 + 100j, True),
    (-0.1 + 50j, False),
    (50 + 100.1j, False),
])
def testInBoundsIncludesTheEdges(pt, expected):
    assert _inBounds(pt, (0.0, 0.0, 100.0, 100.0)) is expected


#endregion

#region splitting at canvas edge


_BOUNDS = (0.0, 0.0, 100.0, 100.0)

def testFullyInsideAndFullyOutsideKeepTheOriginalObject():
    """Both bbox fast paths hand the segment straight back - no subsegment() call, so
    an untouched Arc keeps its exact parameters."""
    inside = Line(10 + 10j, 20 + 20j)
    assert _splitAtBounds(inside, _BOUNDS) == [(inside, True)]

    outside = Line(200 + 200j, 300 + 300j)
    assert _splitAtBounds(outside, _BOUNDS) == [(outside, False)]

def testLineCrossingOneEdgeSplitsInTwo():
    pieces = _splitAtBounds(Line(50 + 50j, 150 + 50j), _BOUNDS)
    assert [inB for _, inB in pieces] == [True, False]
    assert pieces[0][0].point(1) == pytest.approx(100 + 50j), "the split lands on the edge"

def testLineCrossingOppositeEdgesSplitsInThree():
    pieces = _splitAtBounds(Line(-50 + 50j, 150 + 50j), _BOUNDS)
    assert [inB for _, inB in pieces] == [False, True, False]
    assert pieces[1][0].point(0) == pytest.approx(0 + 50j)
    assert pieces[1][0].point(1) == pytest.approx(100 + 50j)

def testTangentTouchesDoNotSplitTheSegment():
    """A line through the corner exactly touches the rect but is outside either side of
    it - the two crossings collapse and the same-side runs merge back into one piece."""
    corner = Line(50 - 50j, 150 + 50j)
    assert _splitAtBounds(corner, _BOUNDS) == [(corner, False)]

def testASegmentAlongAnEdgeCountsAsInside():
    edge = Line(0 + 0j, 100 + 0j)
    assert _splitAtBounds(edge, _BOUNDS) == [(edge, True)]

def testArcSplitsIntoContiguousPiecesCoveringTheWhole():
    """A circle centred on the right edge leaves and re-enters. The pieces must join up
    exactly and still start and end where the original arc did."""
    arc = Arc(center=100 + 50j, u=30 + 0j, v=0 + 30j)
    pieces = _splitAtBounds(arc, _BOUNDS)
    assert [inB for _, inB in pieces] == [False, True, False]
    for (a, _), (b, _) in zip(pieces, pieces[1:]):
        assert a.point(1) == pytest.approx(b.point(0))
    assert pieces[0][0].point(0) == pytest.approx(arc.point(0))
    assert pieces[-1][0].point(1) == pytest.approx(arc.point(1))


#endregion

#region path emission


def _polygon(sides: int, lineType: LineType = LineType.STROKE) -> PathObject:
    """A regular polygon centred on the canvas, so nothing is cropped and no two
    adjacent segments are collinear - a straight run would be merged back into one
    segment by tessellate()."""
    pts = [50 + 50j + 20 * cmath.exp(2j * math.pi * k / sides) for k in range(sides)]
    pts.append(pts[0])
    return PathObject("poly", [Path([Line(pts[i], pts[i + 1]) for i in range(sides)], lineType)])

def _features(settings: Settings, obj: PathObject) -> list[str]:
    return [l.removeprefix("; FEATURE: ") for l in _emit(lambda st, f: _addPath(st, settings, obj, f))
            if l.startswith("; FEATURE:")]

def testRawGeometryIsNeverDrawn():
    """dropRawGeometry should have removed these already; _addPath skips them anyway."""
    assert _emit(lambda st, f: _addPath(st, _settings(), _polygon(4, LineType.RAW_GEOMETRY), f)) == []

def testAddPathReturnsTrueWhenItDrawsSomething():
    assert _addPath(_state(), _settings(), _polygon(4), io.StringIO()) is True

def testAddPathReturnsFalseWhenNothingIsDrawn():
    """createFile relies on this to decide whether an object earns a layerChangeMessage
    comment - both a RAW_GEOMETRY-only object and one cropped out of the canvas
    entirely must report back that they wrote nothing."""
    assert _addPath(_state(), _settings(), _polygon(4, LineType.RAW_GEOMETRY), io.StringIO()) is False
    assert _addPath(_state(), _settings(), _offscreenObject("gone"), io.StringIO()) is False

@pytest.mark.parametrize("sides", [3, 4, 5, 6, 7])
def testAClosedPathNeverEndsOnTheColourItStartedWith(sides):
    """style=="segment" cycles once per segment with no notion of closure, so a segment
    count that doesn't divide evenly would wrap the last segment back onto the first's
    colour - and they're adjacent, so the join would visually disappear."""
    settings = _settings(style="segment", segmentTypes=_SEGMENT_TYPES)
    features = _features(settings, _polygon(sides))
    assert len(features) == sides
    assert features[0] != features[-1]

def testTheFourOverThreeCaseLandsOneColourFurtherOut():
    """The concrete shape of the fix: a-b-c-b, not a-b-c-a."""
    settings = _settings(style="segment", segmentTypes=_SEGMENT_TYPES)
    assert _features(settings, _polygon(4)) == ["a", "b", "c", "b"]

def testCroppedPiecesAreDroppedAndTheObjectIsNamed():
    settings = _settings(showOutOfBounds=False)
    obj = PathObject("spill", [Path([Line(50 + 50j, 200 + 50j)], LineType.STROKE)])
    names: list[str] = []
    lines = _emit(lambda st, f: _addPath(st, settings, obj, f, False, names))
    assert names == ["spill"]
    assert lines[-1] == "G1 X100 E50", "stops at the canvas edge; nothing beyond it"

def testMarkedPiecesAreDrawnAsInvalidInstead():
    settings = _settings(showOutOfBounds=True)
    obj = PathObject("spill", [Path([Line(50 + 50j, 200 + 50j)], LineType.STROKE)])
    names: list[str] = []
    lines = _emit(lambda st, f: _addPath(st, settings, obj, f, False, names))
    assert names == ["spill"], "still reported, even though nothing was physically cropped"
    assert lines[-2:] == ["; FEATURE: Invalid", "G1 X200 E100"]

def testAnObjectIsOnlyNamedOnce():
    """The warning names objects, not segments - one entry however much is lost."""
    pts = [50 + 50j, 200 + 50j, 200 + 80j, 50 + 80j]
    obj = PathObject("spill", [Path([Line(pts[i], pts[i + 1]) for i in range(3)], LineType.STROKE)])
    names: list[str] = []
    _emit(lambda st, f: _addPath(st, _settings(), obj, f, False, names))
    assert names == ["spill"]


#endregion

#region per-object motion overrides


def _line(objId: str = "o", overrides: dict[str, float] | None = None) -> PathObject:
    return PathObject(objId, [Path([Line(40 + 40j, 60 + 40j)], LineType.STROKE)],
                      overrides=overrides or {})

_MOTION_SETTINGS = dict(speeds={LineType.STROKE: 600, LineType.TRAVEL: 3000},
                        accels={LineType.STROKE: 500, LineType.TRAVEL: 1000})

def testOverridesReplaceTheSettingsValuesForThatObject():
    settings = _settings(**_MOTION_SETTINGS)
    obj = _line(overrides={"height": 7.0, "speed": 1234.0, "accel": 4321.0})
    lines = _emit(lambda st, f: _addPath(st, settings, obj, f))
    # the travel lift clears the override height (7 > the travel height) with a 1mm
    # margin, so the draw itself still lowers back down to the exact override height
    assert lines == ["M204 S1000", "G1 Z8 F3000", "G1 X40 Y40", "G1 Z7", "M204 S4321", "; FEATURE: Stroke", "G1 X60 E20 F1234"]

def testAnObjectWithoutOverridesStillUsesTheSettingsValues():
    settings = _settings(**_MOTION_SETTINGS)
    lines = _emit(lambda st, f: _addPath(st, settings, _line(), f))
    assert "M204 S500" in lines
    assert f"G1 Z{_DRAW_Z:g}" in lines

def testOverridesDoNotReachTravelMoveSpeed():
    """A sweep still has to travel between its rows at the configured travel speed -
    the override describes how the object draws, not how the machine gets there."""
    settings = _settings(**_MOTION_SETTINGS)
    obj = _line(overrides={"height": 1.0, "speed": 1234.0})
    lines = _emit(lambda st, f: _addPath(st, settings, obj, f), _state(0, 0, 0))
    assert f"G1 Z{_TRAVEL_Z:g} F3000" in lines, "the pen lift keeps the travel speed"

def testATallHeightOverrideRaisesTheTravelHeightToClearIt():
    """A height override above the configured travel height would otherwise leave the
    travel move dipping back down through the object's own draw height on its way
    between rows - the travel height rises 1mm above it instead, still at travel
    speed."""
    settings = _settings(**_MOTION_SETTINGS)
    obj = _line(overrides={"height": 7.0})
    lines = _emit(lambda st, f: _addPath(st, settings, obj, f), _state(0, 0, 0))
    assert "G1 Z8 F3000" in lines

def testOverridesDoNotLeakIntoTheNextObject():
    settings = _settings(**_MOTION_SETTINGS)
    def emitBoth(st, f):
        _addPath(st, settings, _line("a", {"height": 7.0}), f)
        _addPath(st, settings, _line("b"), f)
    lines = _emit(emitBoth)
    aHeight = next(i for i, l in enumerate(lines) if l.startswith("G1 Z7"))
    bHeight = lines.index(f"G1 Z{_DRAW_Z:g}")
    assert aHeight < bHeight, "b falls back to the settings height"

def testAHeightOverrideSuppressesThePreviewRaise():
    """objectHeightChange's +0.001mm would otherwise land on half the rows of a sheet
    that is specifically sweeping height."""
    settings = _settings(**_MOTION_SETTINGS)
    lines = _emit(lambda st, f: _addPath(st, settings, _line(overrides={"height": 7.0}), f, True))
    assert any(l.startswith("G1 Z7") for l in lines)
    assert not any(l.startswith("G1 Z7.001") for l in lines)

def testAnUnknownOverrideIsReportedAndIgnored(capsys):
    settings = _settings(**_MOTION_SETTINGS)
    lines = _emit(lambda st, f: _addPath(st, settings, _line(overrides={"heigth": 7.0}), f))
    assert "heigth" in capsys.readouterr().out
    assert f"G1 Z{_DRAW_Z:g}" in lines, "the typo'd key changed nothing"


#endregion

#region bed exclude area


_SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]

def _sameCycle(a: list, b: list) -> bool:
    return len(a) == len(b) and any(a[i:] + a[:i] == b for i in range(len(a)))

def testAlreadyMinimalPolygonIsUntouched():
    assert _removeRedundantPoints(list(_SQUARE)) == _SQUARE

@pytest.mark.parametrize("points, label", [
    ([(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)], "midpoint of a straight edge"),
    ([(0.0, 0.0), (0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)], "adjacent duplicate"),
    ([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)], "explicit closing point"),
    ([(0.0, 0.0), (10.0, 0.0), (20.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)], "zero-width seam"),
])
def testRedundantPointsAreRemoved(points, label):
    """Each of these adds a vertex the polygon's outline doesn't need. A seam is
    traversed out and back, so dropping it leaves the visible shape unchanged."""
    assert _sameCycle(_removeRedundantPoints(points), _SQUARE), label

def testFullyDegenerateInputCollapsesToOnePoint():
    """The collinearity loop stops at 2 points, so an all-coincident input would
    otherwise bottom out as a duplicate pair."""
    assert _removeRedundantPoints([(3.0, 4.0)] * 4) == [(3.0, 4.0)]

@pytest.mark.parametrize("points", [[], [(1.0, 2.0)], [(0.0, 0.0), (1.0, 1.0)]])
def testTooFewPointsToReduceArePassedThrough(points):
    assert _removeRedundantPoints(list(points)) == points

def testBridgeContoursJoinsAtTheClosestPairAndKeepsEveryPoint():
    """The hole sits up by the outer square's far corner, so the seam is nowhere near
    either contour's first point - a join that ignored the distances would land
    elsewhere."""
    outer = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    hole = [(6.0, 6.0), (6.0, 9.0), (9.0, 9.0), (9.0, 6.0)]
    joined = _bridgeContours(outer, hole)
    assert set(joined) == set(outer) | set(hole)
    assert joined.count((10.0, 10.0)) == 2 and joined.count((9.0, 9.0)) == 2, "the seam is walked twice"

_PLATE_CORNERS = [(0.0, 0.0), (150.0, 0.0), (150.0, 150.0), (0.0, 150.0)]
_CANVAS_CORNERS = [(50.0, 50.0), (100.0, 50.0), (100.0, 100.0), (50.0, 100.0)]

@pytest.mark.parametrize("gaps, expected", [
    # one gap: a strip along that plate edge, bracketed by the canvas corners on it
    ([True, False, False, False], [(50.0, 50.0), (0.0, 0.0), (150.0, 0.0), (100.0, 50.0)]),
    ([False, True, False, False], [(100.0, 50.0), (150.0, 0.0), (150.0, 150.0), (100.0, 100.0)]),
    # two adjacent gaps: an L, walking the plate across both before returning
    ([True, True, False, False], [(50.0, 50.0), (0.0, 0.0), (150.0, 0.0), (150.0, 0.0),
                                  (150.0, 150.0), (100.0, 100.0), (100.0, 50.0)]),
])
def testPerimeterWalkTracesTheGapRun(gaps, expected):
    assert _perimeterWalk(gaps, _PLATE_CORNERS, _CANVAS_CORNERS) == expected

_PLATE = 150 + 150j

def _parsePolygon(area: str) -> list[tuple[float, float]]:
    return [] if not area else [(float(p.split("x")[0]), float(p.split("x")[1])) for p in area.split(",")]

# even-odd ray cast. the bridged contours are walked out and back along a zero-width
# seam, so a ray crosses it twice and the parity is unaffected
def _insidePolygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % len(poly)]
        if (y0 > y) != (y1 > y) and x0 + (y - y0) * (x1 - x0) / (y1 - y0) > x:
            inside = not inside
    return inside

# every way an axis-aligned canvas can sit inside an axis-aligned plate, named by which
# plate edges it fails to reach - the classification _bedExcludeArea branches on
_CANVAS_CASES = [
    (0 + 0j, 150 + 150j, "no gaps: canvas covers the plate"),
    (50 + 50j, 100 + 100j, "4 gaps: an island, so a ring"),
    (50 + 0j, 100 + 150j, "left+right: two vertical strips"),
    (0 + 50j, 150 + 100j, "bottom+top: two horizontal strips"),
    (0 + 50j, 150 + 150j, "bottom only"),
    (50 + 0j, 150 + 150j, "left only"),
    (50 + 50j, 150 + 150j, "bottom+left: an L"),
    (0 + 50j, 100 + 100j, "bottom+right+top: a C"),
    (200 + 200j, 300 + 300j, "canvas off the plate entirely"),
    (-20 - 20j, 100 + 100j, "canvas overhanging, clamped back in"),
]

@pytest.mark.parametrize("canvasMin, canvasMax, label", _CANVAS_CASES)
def testExcludeAreaCoversExactlyThePlateMinusTheCanvas(canvasMin, canvasMax, label):
    """The one property that matters, asserted the way the slicer reads it: a point is
    in the polygon iff it's on the plate but off the canvas. Sampling on a 2.5mm grid
    offset by half, so no sample lands on an edge."""
    polygon = _parsePolygon(_bedExcludeArea(_PLATE, canvasMin, canvasMax))
    cx0, cy0 = max(0.0, canvasMin.real), max(0.0, canvasMin.imag)
    cx1, cy1 = min(150.0, canvasMax.real), min(150.0, canvasMax.imag)
    for i in range(60):
        for j in range(60):
            x, y = 0.5 + 2.5 * i, 0.5 + 2.5 * j
            onCanvas = cx0 < x < cx1 and cy0 < y < cy1
            assert _insidePolygon(x, y, polygon) is not onCanvas, f"({x}, {y}) in {label}"

def testACanvasCoveringThePlateExcludesNothing():
    assert _bedExcludeArea(_PLATE, 0j, 150 + 150j) == ""

def testACanvasOffThePlateExcludesAllOfIt():
    assert _bedExcludeArea(_PLATE, 200 + 200j, 300 + 300j) == "0x0,150x0,150x150,0x150"

@pytest.mark.parametrize("canvasMin, canvasMax", [
    (-20 - 20j, 100 + 100j),    # over the bottom-left
    (50 + 50j, 200 + 200j),     # over the top-right
    (-20 + 50j, 200 + 100j),    # out both sides at once
])
def testAnOverhangingCanvasIsClampedIntoThePlate(canvasMin, canvasMax):
    """An out-of-bounds canvas is only warned about at load time, never corrected, so
    it arrives here intact - but the polygon handed to the slicer still has to stay on
    the bed."""
    polygon = _parsePolygon(_bedExcludeArea(_PLATE, canvasMin, canvasMax))
    assert all(0 <= x <= 150 and 0 <= y <= 150 for x, y in polygon)

@pytest.mark.parametrize("canvasMin, canvasMax, label", _CANVAS_CASES)
def testExcludeAreaEnclosesTheRightSignedArea(canvasMin, canvasMax, label):
    """The same property read under the *nonzero* rule, which even-odd sampling is
    blind to: the hole and the strips are wound against the plate, so the shoelace
    areas subtract rather than pile up."""
    polygon = _parsePolygon(_bedExcludeArea(_PLATE, canvasMin, canvasMax))
    signed = 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1)
                       in zip(polygon, polygon[1:] + polygon[:1]))
    cx0, cy0 = max(0.0, canvasMin.real), max(0.0, canvasMin.imag)
    cx1, cy1 = min(150.0, canvasMax.real), min(150.0, canvasMax.imag)
    onPlate = max(0.0, cx1 - cx0) * max(0.0, cy1 - cy0)
    assert signed == pytest.approx(150 * 150 - onPlate), label

@pytest.mark.parametrize("canvasMin, canvasMax, label", _CANVAS_CASES)
def testExcludeAreaCarriesNoRedundantPoints(canvasMin, canvasMax, label):
    polygon = _parsePolygon(_bedExcludeArea(_PLATE, canvasMin, canvasMax))
    assert _removeRedundantPoints(polygon) == polygon, label

def testPrintableAreaFormatsThePlatesOwnFourCorners():
    """Unlike _bedExcludeArea, printable_area always names the whole plate -
    there's no canvas rect to subtract."""
    assert _printableArea(256 + 256j) == "0x0,256x0,256x256,0x256"


#endregion

#region pen-load wait


def _block(delay: float, showProgress: bool = True) -> str:
    return _waitForPen(Settings(loadDelay=delay, showLoadProgress=showProgress))

def _dwells(block: str) -> list[float]:
    return [float(l[4:]) for l in block.splitlines() if l.startswith("G4 P")]

def _percents(block: str) -> list[int]:
    return [int(l[5:]) for l in block.splitlines() if l.startswith("M73 P")]

# 0.4s/5s: 5% is the coarsest step allowed, so both step a clean 5% (at 20ms/250ms).
# 20s: 500ms is the shorter cap, so it steps by time and the percentages land at 2.5%.
# 20.3s: doesn't divide into whole milliseconds - the case that catches per-step
#        rounding drift, which every other delay here hides by dividing evenly.
# 400s: 500ms would be an eighth of a percent, so the 1% floor takes over instead.
_DELAYS = [0.4, 5, 20, 20.3, 400]

@pytest.mark.parametrize("delay", _DELAYS)
def testLoadProgressCountsDownFromFullToEmpty(delay):
    """The countdown shows time *remaining*, so it starts at 100 and lands on 0
    without ever repeating or going back up."""
    percents = _percents(_block(delay))
    assert percents[0] == 100
    assert percents[-1] == 0
    assert all(a > b for a, b in zip(percents, percents[1:])), "must decrease monotonically"

@pytest.mark.parametrize("delay", _DELAYS)
def testLoadProgressWaitsExactlyTheConfiguredDelay(delay):
    """Splitting the dwell must not change how long it lasts. Each step is rounded to
    whole milliseconds, so this only holds because the boundaries are taken from the
    cumulative time rather than by summing a rounded per-step interval."""
    assert sum(_dwells(_block(delay))) == pytest.approx(delay * 1000)

@pytest.mark.parametrize("delay", _DELAYS)
def testLoadProgressStepsWithinBothBounds(delay):
    """The step is the 500ms tick clamped into 1%..5%: fine enough to look like a
    countdown, coarse enough not to re-emit a number the screen already shows."""
    steps = len(_dwells(_block(delay)))
    assert _LOAD_PROGRESS_MIN_STEP <= 100 / steps <= _LOAD_PROGRESS_MAX_STEP

@pytest.mark.parametrize("delay", [0.4, 5, 20, 20.3])
def testLoadProgressTicksAtLeastEveryHalfSecond(delay):
    """Under the 1% floor's reach, every tick is also inside the time cap. (400s is
    excluded: there the floor deliberately wins and ticks run 4s apart.)"""
    assert max(_dwells(_block(delay))) <= _LOAD_PROGRESS_MAX_INTERVAL * 1000 + 1 # +1ms for whole-ms rounding

def testVeryLongDelayFallsBackToTheOnePercentFloor():
    """At 400s a 500ms tick is an eighth of a percent. The floor takes over, giving
    exactly the 101 whole percentages - not 800 ticks mostly repeating each other."""
    block = _block(400)
    assert _percents(block) == list(range(100, -1, -1))
    assert _dwells(block) == [4000] * 100

def testLoadProgressOffWaitsSilently():
    """Disabled, it's the plain dwell it always was - just G4 P (ms) rather than G4 S."""
    assert _block(5, showProgress=False) == "G4 P5000"

@pytest.mark.parametrize("delay", [0, -3])
def testNonPositiveDelayEmitsNoCountdown(delay):
    """Nothing to count down through - and a negative delay must not become a
    negative dwell."""
    assert _block(delay) == "G4 P0"


#endregion

#region whole-file output


@pytest.fixture
def templates(tmp_path):
    """A slicer prefix/suffix pair covering every substitution createFile supplies,
    plus a trivial machine prefix/suffix pair - createFile nests the machine
    templates' rendered text into the slicer ones via {MACHINE_PREFIX}/
    {MACHINE_SUFFIX}, so both call sites need a real file to open even in tests
    that don't otherwise care about the machine side."""
    prefix = tmp_path / "prefix.gcode"
    prefix.write_text("; speed={TRAVEL_SPEED} half={TRAVEL_SPEED/2} width={LINE_WIDTH}\n"
                      "; offset={EXTRUDER_OFFSET}\n; exclude={BED_EXCLUDE_AREA}\n"
                      "{MACHINE_PREFIX}")
    suffix = tmp_path / "suffix.gcode"
    suffix.write_text("{MACHINE_SUFFIX}; end={END_X},{END_Y}\n")
    machinePrefix = tmp_path / "machine_prefix.gcode"
    machinePrefix.write_text("; machine prefix\n")
    machineSuffix = tmp_path / "machine_suffix.gcode"
    machineSuffix.write_text("; machine suffix\n")
    return prefix, suffix, machinePrefix, machineSuffix

def _document() -> Document:
    doc = Document()
    doc.add(PathObject("a", [Path([Line(30 + 30j, 60 + 60j)], LineType.STROKE)]))
    return doc

def _fileSettings(templates, **overrides) -> Settings:
    slicerPrefix, slicerSuffix, machinePrefix, machineSuffix = templates
    base = dict(slicerPrefixFile=str(slicerPrefix), slicerSuffixFile=str(slicerSuffix),
                machinePrefixFile=str(machinePrefix), machineSuffixFile=str(machineSuffix),
                penWidth=0.4, endPos=5 + 6j,
                speeds={LineType.STROKE: 600, LineType.TRAVEL: 3000},
                accels={LineType.STROKE: 500, LineType.TRAVEL: 1000},
                plateSize=150 + 150j, canvasOffset=25 + 25j)
    return _settings(**{**base, **overrides})

def testCreateFileWritesPrefixBodyAndSuffix(tmp_path, templates):
    out = tmp_path / "out.gcode"
    assert createFile(_document(), _fileSettings(templates), str(out)) is True
    text = out.read_text()
    assert text.startswith("; speed=3000 half=1500 width=0.4\n")
    assert "; end=5,6\n" in text
    assert "G1 X60 Y60 E42.42641" in text, "the body sits between them"

def testMachineTemplatesAreNestedInsideSlicerTemplates(tmp_path, templates):
    """{MACHINE_PREFIX}/{MACHINE_SUFFIX} in the slicer templates are replaced with
    the machine templates' own rendered text - the placeholders themselves never
    reach the output file."""
    out = tmp_path / "out.gcode"
    createFile(_document(), _fileSettings(templates), str(out))
    text = out.read_text()
    assert "; machine prefix" in text
    assert "; machine suffix" in text
    assert "{MACHINE_PREFIX}" not in text
    assert "{MACHINE_SUFFIX}" not in text

def testPenOffsetGoesToTheSlicerWhenShowingPenPositions(tmp_path, templates):
    """showPenPos hands the real offset to the slicer, which then applies it itself -
    so the canvas rect stays in pen space. Off, the offset is faked and the rect is
    shifted into nozzle space instead."""
    out = tmp_path / "out.gcode"
    createFile(_document(), _fileSettings(templates, showPenPos=True, penOffset=4 + 3j), str(out))
    assert "; offset=4x3" in out.read_text()

    createFile(_document(), _fileSettings(templates, showPenPos=False, penOffset=4 + 3j), str(out))
    text = out.read_text()
    assert "; offset=0x2" in text, "bambu studio's default"
    assert ",21x22," in text, "the canvas corner, shifted by -penOffset"

def testAFailedWriteLeavesTheExistingFileIntact(tmp_path, templates, capsys):
    """The whole point of the temp-file swap: a crash partway through (here a missing
    suffix template) must not truncate whatever was already there."""
    out = tmp_path / "out.gcode"
    out.write_text("PRECIOUS")
    settings = _fileSettings(templates, slicerSuffixFile=str(tmp_path / "missing.gcode"))
    assert createFile(_document(), settings, str(out)) is False
    assert out.read_text() == "PRECIOUS"
    assert "missing.gcode" in capsys.readouterr().out, "the message must name a path the user typed"

def testFailedWritesLeaveNoTempFilesBehind(tmp_path, templates):
    out = tmp_path / "out.gcode"
    settings = _fileSettings(templates, slicerSuffixFile=str(tmp_path / "missing.gcode"))
    createFile(_document(), settings, str(out))
    assert list(tmp_path.glob("*.tmp")) == []

def testAnUnwritableDirectoryIsReportedAgainstTheUsersPath(tmp_path, templates, capsys):
    """mkstemp fails on the directory, but the user only ever typed the output file -
    a temp path inside it would mean nothing to them."""
    out = tmp_path / "nonexistent" / "out.gcode"
    assert createFile(_document(), _fileSettings(templates), str(out)) is False
    assert str(out) in capsys.readouterr().out

def testOutOfBoundsObjectsAreReportedOnce(tmp_path, templates, capsys):
    """One combined warning per run, not one per object or per out-of-bounds segment:
    "first" has two out-of-bounds segments but must still be named only once, and
    "second" must land in that same warning rather than getting its own."""
    doc = Document()
    doc.add(PathObject("first", [Path([Line(30 + 30j, 500 + 500j), Line(500 + 500j, 30 + 500j)], LineType.STROKE)]))
    doc.add(PathObject("second", [Path([Line(30 + 30j, -500 - 500j)], LineType.STROKE)]))
    out = tmp_path / "out.gcode"
    createFile(doc, _fileSettings(templates), str(out))
    printed = capsys.readouterr().out

    assert printed.count("outside the canvas") == 1, "a single combined warning, not one per object/segment"
    assert printed.count("first") == 1, "an object with two out-of-bounds segments is still named only once"
    assert "second" in printed

def _offscreenObject(objId: str) -> PathObject:
    """An object entirely outside the default 100x100 canvas - crop mode drops all
    of it, so it draws nothing at all."""
    return PathObject(objId, [Path([Line(-500 - 500j, -400 - 400j)], LineType.STROKE)])

def testEmptyObjectsGetNoLayerChangeComment(tmp_path, templates):
    """An object cropped down to nothing must not get a layer-change comment of its
    own - OrcaSlicer (unlike Bambu Studio) stops rendering every later object once
    it sees one with no draw moves before the next."""
    doc = Document()
    doc.add(_offscreenObject("offscreen"))
    doc.add(PathObject("visible", [Path([Line(30 + 30j, 60 + 60j)], LineType.STROKE)]))
    out = tmp_path / "out.gcode"
    settings = _fileSettings(templates, objectHeightChange=True, layerChangeMessage="; CHANGE_LAYER")
    createFile(doc, settings, str(out))
    assert out.read_text().count("; CHANGE_LAYER") == 1, "only the object that actually drew something gets one"

def testConsecutiveEmptyObjectsProduceNoLayerChangeComments(tmp_path, templates):
    doc = Document()
    doc.add(_offscreenObject("a"))
    doc.add(_offscreenObject("b"))
    out = tmp_path / "out.gcode"
    settings = _fileSettings(templates, objectHeightChange=True, layerChangeMessage="; CHANGE_LAYER")
    createFile(doc, settings, str(out))
    assert "CHANGE_LAYER" not in out.read_text()

def testObjectHeightChangeParityStillCountsEmptyObjects(tmp_path, templates):
    """The raised/not-raised alternation is based on an object's position in
    geom.objects, not on how many objects actually drew something - simplest to
    reason about, and untouched by skipping the empty ones' layer-change comment."""
    doc = Document()
    doc.add(_offscreenObject("empty")) # index 0 - "raised", but draws nothing
    doc.add(PathObject("visible", [Path([Line(30 + 30j, 60 + 60j)], LineType.STROKE)])) # index 1 - not raised
    out = tmp_path / "out.gcode"
    settings = _fileSettings(templates, objectHeightChange=True)
    createFile(doc, settings, str(out))
    assert f"G1 Z{_DRAW_Z + .001:g}" not in out.read_text()


#endregion


#region shipped template sanity

def testEveryShippedSlicerTemplateNestsTheMatchingMachinePlaceholder():
    """createFile renders the machine templates first, then substitutes their text into
    the slicer templates via {MACHINE_PREFIX}/{MACHINE_SUFFIX} - a slicer prefix missing
    the former (or a suffix missing the latter) would silently drop the machine's own
    start/end gcode (homing, the pen-load dwell, park moves) from the output file."""
    for path in sorted(glob.glob("gcode_templates/slicers/*_prefix.gcode")):
        text = pathlib.Path(path).read_text()
        assert "{MACHINE_PREFIX}" in text, f"{path} has no {{MACHINE_PREFIX}} placeholder"
    for path in sorted(glob.glob("gcode_templates/slicers/*_suffix.gcode")):
        text = pathlib.Path(path).read_text()
        assert "{MACHINE_SUFFIX}" in text, f"{path} has no {{MACHINE_SUFFIX}} placeholder"

#endregion
