"""Unit tests for lib/calibration.py: the shared framework (input prompts, ramp/layout
helpers, the stroke font, the test registry/entry point) and each individual
calibration test as it's added.
"""
import glob

import pytest

from lib.calibration import (
    CALIBRATION_TESTS, Pass, _GLYPH_ADVANCE, _GLYPH_WIDTH, _GLYPHS, _HEIGHT_TEST_LINE_LENGTH,
    _HEIGHT_TEST_TICK_5, _HEIGHT_TEST_TICK_10, _LABEL_CAP_HEIGHT, _centerPasses, _heightTest,
    _rampValues, _textPaths, _textWidth, _translatePaths, calibrationEnabled,
    generateCalibration, promptNumber, promptRamp,
)
from lib.geometry import Line, Path
from lib.plot import _canvasBoundsNozzle, createFile
from lib.settings import LineType, Settings


#region input handling

def _feedInputs(monkeypatch, *responses: str):
    """Makes input() return each of responses in turn, regardless of the prompt text."""
    it = iter(responses)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))

def testPromptNumberAcceptsAValidValue(monkeypatch):
    _feedInputs(monkeypatch, "3.5")
    assert promptNumber("x: ") == 3.5

def testPromptNumberAcceptsBoundaryValues(monkeypatch):
    _feedInputs(monkeypatch, "10")
    assert promptNumber("x: ", lo=0, hi=10) == 10

def testPromptNumberReAsksOnNonNumericInput(monkeypatch, capsys):
    _feedInputs(monkeypatch, "abc", "5")
    assert promptNumber("x: ") == 5
    assert "'abc' is not a number" in capsys.readouterr().out

def testPromptNumberReAsksOnEmptyInput(monkeypatch, capsys):
    _feedInputs(monkeypatch, "", "5")
    assert promptNumber("x: ") == 5
    assert "is not a number" in capsys.readouterr().out

def testPromptNumberReAsksOnOutOfRangeInput(monkeypatch, capsys):
    _feedInputs(monkeypatch, "100", "5")
    assert promptNumber("x: ", lo=0, hi=10) == 5
    assert "between 0 and 10" in capsys.readouterr().out

def testPromptNumberMessageWhenOnlyAnUpperBoundIsSet(monkeypatch, capsys):
    _feedInputs(monkeypatch, "20", "5")
    assert promptNumber("x: ", hi=10) == 5
    assert "below 10" in capsys.readouterr().out

def testPromptNumberMessageWhenOnlyALowerBoundIsSet(monkeypatch, capsys):
    _feedInputs(monkeypatch, "-5", "5")
    assert promptNumber("x: ", lo=0) == 5
    assert "above 0" in capsys.readouterr().out

def testPromptNumberRejectsNonFiniteInput(monkeypatch, capsys):
    _feedInputs(monkeypatch, "inf", "5")
    assert promptNumber("x: ") == 5
    assert "is not a number" in capsys.readouterr().out

def testPromptRampReturnsTheTrioAndPrintsThePassCount(monkeypatch, capsys):
    _feedInputs(monkeypatch, "1", "2", "0.5")
    assert promptRamp("height", "mm") == (1.0, 2.0, 0.5)
    assert "3 passes" in capsys.readouterr().out

def testPromptRampTakesTheAbsoluteValueOfANegativeStep(monkeypatch):
    _feedInputs(monkeypatch, "1", "2", "-0.5")
    assert promptRamp("height", "mm") == (1.0, 2.0, 0.5)

def testPromptRampReAsksTheWholeTrioWhenMaxIsNotGreaterThanMin(monkeypatch, capsys):
    # the rejected attempt only consumes lo/hi - step is never asked for a trio
    # that's already invalid
    _feedInputs(monkeypatch, "5", "5", "5", "10", "1")
    assert promptRamp("height", "mm") == (5.0, 10.0, 1.0)
    assert "greater than the minimum" in capsys.readouterr().out

def testPromptRampReAsksTheWholeTrioOnZeroStep(monkeypatch, capsys):
    _feedInputs(monkeypatch, "1", "2", "0", "1", "2", "0.5")
    assert promptRamp("height", "mm") == (1.0, 2.0, 0.5)
    assert "must not be zero" in capsys.readouterr().out

def testPromptRampReAsksTheWholeTrioWhenBoundsCheckRejects(monkeypatch, capsys):
    """boundsCheck gets one shot at the first (invalid) trio, then accepts the second."""
    calls = []
    def boundsCheck(lo, hi, step):
        calls.append((lo, hi, step))
        return "too many passes for the canvas" if len(calls) == 1 else None
    _feedInputs(monkeypatch, "1", "2", "0.1", "1", "2", "1")
    assert promptRamp("height", "mm", boundsCheck) == (1.0, 2.0, 1.0)
    assert "too many passes for the canvas" in capsys.readouterr().out
    assert calls == [(1.0, 2.0, 0.1), (1.0, 2.0, 1.0)]

#endregion


#region _rampValues

def testRampValuesIncludesHiWhenStepDividesEvenly():
    assert _rampValues(1, 2, 0.5) == [1, 1.5, 2.0]

def testRampValuesNeverOvershootsHi():
    values = _rampValues(1, 2, 0.3)
    assert values == pytest.approx([1, 1.3, 1.6, 1.9])
    assert values[-1] < 2

def testRampValuesTreatsANegativeStepAsItsAbsoluteValue():
    assert _rampValues(1, 2, -0.5) == _rampValues(1, 2, 0.5)

#endregion


#region layout helpers

def testTranslatePathsShiftsEverySegment():
    path = Path([Line(0j, 1 + 1j)], LineType.STROKE)
    _translatePaths([path], 2 + 3j)
    segment = path.segments[0]
    assert isinstance(segment, Line)
    assert segment.start == 2 + 3j
    assert segment.end == 3 + 4j

def testCenterPassesTranslatesGeometryAndLabelOriginTogether():
    # pattern spans x:[0,4] y:[0,2] across both passes - center (2,1)
    p1 = Pass("a", [Path([Line(0j, 4 + 0j)], LineType.STROKE)], labelOrigin=1 + 0j)
    p2 = Pass("b", [Path([Line(0 + 2j, 4 + 2j)], LineType.STROKE)])
    bounds = (10.0, 10.0, 20.0, 30.0) # canvas center (15, 20)
    _centerPasses([p1, p2], bounds)

    p1Segment, p2Segment = p1.geometry[0].segments[0], p2.geometry[0].segments[0]
    assert isinstance(p1Segment, Line) and isinstance(p2Segment, Line)
    assert p1Segment.start == pytest.approx(13 + 19j)
    assert p1Segment.end == pytest.approx(17 + 19j)
    assert p1.labelOrigin == pytest.approx(14 + 19j)
    assert p2Segment.start == pytest.approx(13 + 21j)

def testCenterPassesLeavesAnEmptyPatternUnchanged():
    assert _centerPasses([], (0.0, 0.0, 10.0, 10.0)) == []

#endregion


#region stroke font

def testEveryDigitAndTheDecimalPointHasAGlyph():
    for ch in "0123456789.":
        assert ch in _GLYPHS
        assert len(_GLYPHS[ch]) > 0

def testTextPathsScalesAndPositionsAGlyph():
    origin, capHeight = 2 + 3j, 5.0
    paths = _textPaths("0", origin, capHeight) # "0" lights the full glyph box
    assert len(paths) == 1
    xmin, ymin, xmax, ymax = paths[0].bounds()
    assert (xmin, ymin) == pytest.approx((origin.real, origin.imag))
    assert (xmax, ymax) == pytest.approx((origin.real + _GLYPH_WIDTH * capHeight, origin.imag + capHeight))

def testTextPathsAdvancesTheCursorBetweenGlyphs():
    # "1" is a single stroke, so each glyph in "11" is its own Path - compare the
    # second glyph's own bounds to the first's, shifted by one glyph advance
    capHeight = 5.0
    firstGlyph, secondGlyph = _textPaths("11", 0j, capHeight)
    assert secondGlyph.bounds()[0] == pytest.approx(firstGlyph.bounds()[0] + _GLYPH_ADVANCE * capHeight)

def testTextPathsOnUnrenderableTextReturnsNothing():
    assert _textPaths("", 0j, 5.0) == []
    assert _textPaths(" ", 0j, 5.0) == [] # no glyph for a space; nothing to draw

def testTextWidthMatchesAdvanceRegardlessOfGlyphShape():
    # the cursor advances a fixed amount per character - a narrow glyph like "1"
    # and an unrenderable one like " " take up the same space
    assert _textWidth("1", 5.0) == _textWidth(" ", 5.0)
    assert _textWidth("11", 5.0) == pytest.approx(2 * _textWidth("1", 5.0))

#endregion


#region generateCalibration

@pytest.fixture
def registeredTest():
    """Registers a trivial fake calibration test for one test's duration, and removes
    it afterward - CALIBRATION_TESTS is shared module state, so a test that adds to
    it must not leak that entry into whichever test runs next."""
    name = "faketest"
    CALIBRATION_TESTS[name] = lambda settings: [
        Pass("1.00", [Path([Line(0j, 5 + 0j)], LineType.STROKE)], labelOrigin=0j)
    ]
    yield name
    del CALIBRATION_TESTS[name]

@pytest.fixture
def registeredSweep():
    """A two-pass sweep: the first overrides all three motion settings, the second
    only height - so a None field can be told apart from an overridden one."""
    name = "fakesweep"
    CALIBRATION_TESTS[name] = lambda settings: [
        Pass("1", [Path([Line(0j, 5 + 0j)], LineType.STROKE)], height=1.0, speed=600.0, accel=50.0),
        Pass("2", [Path([Line(0 + 5j, 5 + 5j)], LineType.STROKE)], height=2.0),
    ]
    yield name
    del CALIBRATION_TESTS[name]

def testGenerateCalibrationTurnsEachPassIntoAnObjectWithItsOverrides(registeredSweep):
    doc = generateCalibration(Settings(calibrationTest=registeredSweep))
    assert [o.id for o in doc.objects] == ["1", "2"]
    assert doc.objects[0].overrides == {"height": 1.0, "speed": 600.0, "accel": 50.0}
    assert doc.objects[1].overrides == {"height": 2.0}, "a None field is simply not overridden"

def testGenerateCalibrationEmitsALabelObjectOnlyWhereLabelOriginIsSet(registeredTest):
    doc = generateCalibration(Settings(calibrationTest=registeredTest))
    assert [o.id for o in doc.objects] == ["1.00", "1.00 label"]

def testGenerateCalibrationLabelTakesOnlyThePassHeight():
    """The label must fade with its own row (height), but stay legible whatever
    speed/accel the sheet is sweeping - so those two are deliberately not carried."""
    CALIBRATION_TESTS["labelled"] = lambda s: [
        Pass("9", [Path([Line(0j, 5 + 0j)], LineType.STROKE)], labelOrigin=0j,
             height=3.0, speed=999.0, accel=888.0)
    ]
    try:
        doc = generateCalibration(Settings(calibrationTest="labelled"))
    finally:
        del CALIBRATION_TESTS["labelled"]
    assert doc.objects[1].overrides == {"height": 3.0}

#endregion


#region registry / calibrationEnabled

@pytest.fixture
def calibTemplates(tmp_path):
    """A trivial machine/slicer prefix+suffix pair covering the substitutions
    createFile always supplies, as absolute paths so the test doesn't depend on
    the shipped default_*.gcode templates or the process's working directory."""
    machinePrefix = tmp_path / "machine_prefix.gcode"
    machinePrefix.write_text("; machine prefix\n")
    machineSuffix = tmp_path / "machine_suffix.gcode"
    machineSuffix.write_text("; machine suffix\n")
    slicerPrefix = tmp_path / "slicer_prefix.gcode"
    slicerPrefix.write_text("{MACHINE_PREFIX}")
    slicerSuffix = tmp_path / "slicer_suffix.gcode"
    slicerSuffix.write_text("{MACHINE_SUFFIX}")
    return dict(machinePrefixFile=str(machinePrefix), machineSuffixFile=str(machineSuffix),
                slicerPrefixFile=str(slicerPrefix), slicerSuffixFile=str(slicerSuffix))

def _calibSettings(calibTemplates, **overrides) -> Settings:
    base = dict(
        heights={LineType.STROKE: 2.0, LineType.TRAVEL: 5.0},
        speeds={LineType.STROKE: 600.0, LineType.TRAVEL: 3000.0},
        accels={LineType.STROKE: 500.0, LineType.TRAVEL: 1000.0},
        shortTravelThresholds={LineType.STROKE: 0.5},
        canvasSize=100 + 100j,
        **calibTemplates,
    )
    return Settings(**{**base, **overrides})

def testCalibrationSheetSurvivesTheRealCreateFile(tmp_path, monkeypatch, calibTemplates, registeredTest):
    """generateCalibration's Document is an ordinary Document - it goes through the
    same createFile a parsed SVG does, with no calibration-specific write path."""
    monkeypatch.chdir(tmp_path)
    settings = _calibSettings(calibTemplates, calibrationTest=registeredTest)
    out = tmp_path / f"test_{registeredTest}.gcode"
    assert createFile(generateCalibration(settings), settings, str(out)) is True
    text = out.read_text()
    assert "; machine prefix" in text
    assert "; machine suffix" in text
    assert "G1 X5" in text, "the fake test's line endpoint"

def testCalibrationEnabledIsFalseForNone(capsys):
    assert calibrationEnabled(Settings(calibrationTest="none")) is False
    assert capsys.readouterr().out == "", "the default must not warn about anything"

def testCalibrationEnabledIsTrueForARegisteredTest(registeredTest):
    assert calibrationEnabled(Settings(calibrationTest=registeredTest)) is True

def testCalibrationEnabledRejectsAnUnregisteredName(capsys):
    assert calibrationEnabled(Settings(calibrationTest="not_a_real_test")) is False
    warned = capsys.readouterr().out
    assert "not_a_real_test" in warned
    assert "'none'" in warned

def testCalibrationEnabledListsRegisteredNamesInTheWarning(capsys, registeredTest):
    calibrationEnabled(Settings(calibrationTest="not_a_real_test"))
    assert registeredTest in capsys.readouterr().out

def testRegistryHasOnlyTheShippedTests():
    assert set(CALIBRATION_TESTS) == {"height"}

#endregion


#region height test

def testHeightTestBuildsOnePassPerRampValue(monkeypatch):
    _feedInputs(monkeypatch, "1", "3", "1")
    passes = _heightTest(Settings())
    assert [p.height for p in passes] == [1.0, 2.0, 3.0]
    assert [p.label for p in passes] == ["1", "2", "3"]

def testHeightTestStacksLowestValueAtTheBottom(monkeypatch):
    _feedInputs(monkeypatch, "1", "3", "1")
    passes = _heightTest(Settings())
    ys = []
    for p in passes:
        segment = p.geometry[0].segments[0]
        assert isinstance(segment, Line)
        ys.append(segment.start.imag)
    assert ys == sorted(ys), "ascending height sweep order already puts the lowest Z first"

def testHeightTestPitchScalesWithPenWidth(monkeypatch):
    _feedInputs(monkeypatch, "1", "3", "1")
    passes = _heightTest(Settings(penWidth=0.4))
    ys = []
    for p in passes:
        segment = p.geometry[0].segments[0]
        assert isinstance(segment, Line)
        ys.append(segment.start.imag)
    assert ys[1] - ys[0] == pytest.approx(3.5 * 0.4)

def testHeightTestEveryTenthLineIsLabelledStartingFromTheFirst(monkeypatch):
    _feedInputs(monkeypatch, "1", "12", "1") # 12 passes: indices 0-11
    passes = _heightTest(Settings())
    labelled = [i for i, p in enumerate(passes) if p.labelOrigin is not None]
    assert labelled == [0, 10]

def testHeightTestLabelSitsToTheRightOfItsLine(monkeypatch):
    _feedInputs(monkeypatch, "1", "2", "1") # first pass (index 0) is always labelled
    passes = _heightTest(Settings())
    segment = passes[0].geometry[0].segments[0]
    assert isinstance(segment, Line)
    assert passes[0].labelOrigin is not None
    assert passes[0].labelOrigin.real > max(segment.start.real, segment.end.real)

def testHeightTestLabelIsVerticallyCenteredOnItsLine(monkeypatch):
    _feedInputs(monkeypatch, "1", "2", "1") # first pass (index 0) is always labelled
    passes = _heightTest(Settings())
    segment = passes[0].geometry[0].segments[0]
    assert isinstance(segment, Line)
    assert passes[0].labelOrigin is not None
    assert passes[0].labelOrigin.imag == pytest.approx(segment.start.imag - _LABEL_CAP_HEIGHT / 2)

def testHeightTestEveryFifthAndTenthLineIsLonger(monkeypatch):
    _feedInputs(monkeypatch, "1", "10", "1") # 10 passes: indices 0-9
    passes = _heightTest(Settings())
    lengths = []
    for p in passes:
        segment = p.geometry[0].segments[0]
        assert isinstance(segment, Line)
        lengths.append(abs(segment.end.real - segment.start.real))
    plain = _HEIGHT_TEST_LINE_LENGTH
    assert lengths == pytest.approx([
        plain + _HEIGHT_TEST_TICK_10, plain, plain, plain, plain,
        plain + _HEIGHT_TEST_TICK_5, plain, plain, plain, plain,
    ])

def testHeightTestAlternatesDrawDirectionEveryRow(monkeypatch):
    """Rows draw in alternating directions so the pen only needs a pitch-sized hop
    between consecutive rows, not a full-width trip back to x=0."""
    _feedInputs(monkeypatch, "1", "4", "1")
    passes = _heightTest(Settings())
    for i, p in enumerate(passes):
        segment = p.geometry[0].segments[0]
        assert isinstance(segment, Line)
        if i % 2 == 0:
            assert segment.start.real < segment.end.real
        else:
            assert segment.start.real > segment.end.real

def testHeightTestStaysInsideTheCanvasBounds(monkeypatch):
    _feedInputs(monkeypatch, "1", "10", "1")
    settings = Settings(canvasSize=100 + 100j)
    passes = _heightTest(settings)
    xmin, ymin, xmax, ymax = _canvasBoundsNozzle(settings)
    for p in passes:
        for path in p.geometry:
            pxmin, pymin, pxmax, pymax = path.bounds()
            assert xmin <= pxmin and pxmax <= xmax
            assert ymin <= pymin and pymax <= ymax

def testHeightRoundTripsThroughGenerateCalibration(monkeypatch):
    _feedInputs(monkeypatch, "1", "10", "1") # 10 passes, so only the first gets a label
    doc = generateCalibration(Settings(calibrationTest="height"))
    assert [o.id for o in doc.objects] == ["1", "1 label", "2", "3", "4", "5", "6", "7", "8", "9", "10"]
    assert doc.objects[0].overrides == {"height": 1.0}

#endregion


#region shipped config sanity

def testNoShippedMachineConfigLeavesCalibrationEnabled():
    """A machine config with calibrationTest left set to something other than "none"
    would silently swap every normal run - including CI/manual testing against that
    config - for a calibration sheet instead of a real drawing. Catches a value left
    over from testing a sheet locally and accidentally committed."""
    for path in sorted(glob.glob("config/machines/*.json")):
        settings = Settings()
        settings.initFromMachineJson(path)
        assert settings.calibrationTest == "none", \
            f"{path} has calibrationTest set to '{settings.calibrationTest}'; it must be 'none'"

#endregion
