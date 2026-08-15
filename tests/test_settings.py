"""Unit tests for lib/settings.py.

Settings is the one module the whole pipeline reads from, and it is deliberately
forgiving: a bad value is warned about and skipped, never fatal. That makes it
the easiest place for a regression to hide - a setting that silently stops
applying looks exactly like a setting that was never in the file. So most of
these assert *both* halves: the field kept its default AND something was printed
about it.

Config fixtures that are about the file itself (malformed, wrong shape, wrong
types) live in tests/configs/. The rest are built inline, because one file per
case would be a directory of near-identical JSON that is harder to read than the
dict it came from.
"""
import json
import os
from dataclasses import asdict, fields

import pytest

from lib.settings import _MACHINE_FIELDS, _SLICER_FIELDS, LineType, Settings

CONFIGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")
DRAW_ROLES = (LineType.STROKE, LineType.INFILL, LineType.GAP_INFILL, LineType.INVALID)


#region helpers


def _loader(s: Settings, side: str):
    return s.initFromMachineJson if side == "machine" else s.initFromSlicerJson


def _load(tmp_path, data: dict, side: str = "machine") -> Settings:
    """Write `data` as a config file and load it with the given side's loader."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    s = Settings()
    _loader(s, side)(str(path))
    return s


def _loadFixture(name: str, side: str = "machine") -> Settings:
    s = Settings()
    _loader(s, side)(os.path.join(CONFIGS_DIR, name))
    return s


def _assertDefaultSettings(s: Settings, machineLoaded: bool = False):
    """Every field still matches a fresh Settings() - nothing from the file landed.

    machineLoaded is for a machine config that parses successfully even though
    every individual setting inside it is rejected: initFromMachineJson still
    derives the debug bounding-box heights from the (unchanged, default) draw
    heights on any successful load, so those three keys are expected to differ."""
    expected = Settings()
    if machineLoaded:
        expected.heights[LineType._SEGMENT_BOUNDS] = .1
        expected.heights[LineType._PATH_BOUNDS] = .2
        expected.heights[LineType._DOCUMENT_BOUNDS] = .3
    assert asdict(s) == asdict(expected)


def _warnings(capsys) -> list[str]:
    """Every printed line that reports a problem, ignoring the success banner.

    Every one of initFromMachineJson/initFromSlicerJson's problem messages starts
    with "Warning:", so this is the one thing that filter needs to check."""
    out = capsys.readouterr().out
    return [ln for ln in out.splitlines() if ln.startswith("Warning:")]


#endregion

#region file-level


def testMissingFileFallsBackToDefaults(tmp_path, capsys):
    """A missing config is reported and leaves every field at its default."""
    s = Settings()
    s.initFromMachineJson(str(tmp_path / "nope.json"))
    _assertDefaultSettings(s)
    assert "does not exist" in capsys.readouterr().out


def testMalformedJsonFallsBackToDefaults(capsys):
    """Unparseable JSON is reported and leaves every field at its default."""
    s = _loadFixture("invalid.json")
    _assertDefaultSettings(s)

    out = capsys.readouterr().out
    assert "failed to parse" in out
    # the whole point of the ValueError re-parse in _initFromJson is that the
    # message is one readable line, not a dump of the entire source text
    assert len(out.splitlines()) <= 2, f"parse error should be one line, got:\n{out}"


def testWrongShapeJsonFallsBackToDefaults(capsys):
    """Valid JSON that isn't an object-of-objects is rejected wholesale."""
    s = _loadFixture("not-sections.json")
    _assertDefaultSettings(s)
    assert "must be a JSON object" in capsys.readouterr().out


@pytest.mark.parametrize("data", [
    [1, 2, 3],          # top level is a list
    "settings",         # top level is a scalar
    {"machine": None},  # section is not an object
], ids=["list", "scalar", "null-section"])
def testNonObjectSectionsRejected(tmp_path, data, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    s = Settings()
    s.initFromMachineJson(str(path))
    _assertDefaultSettings(s)
    assert "must be a JSON object" in capsys.readouterr().out


#endregion

#region validation


def testDefaultSettingsAreValid(capsys):
    """The dataclass defaults must not warn about themselves.

    They are what every failure path above falls back to, so a default that
    trips its own validation would mean the fallback is unusable."""
    Settings()._validate()
    assert _warnings(capsys) == []


def testShippedConfigLoadsCleanly(capsys):
    """The real P1S machine config and Bambu Studio slicer config both produce no
    warnings, unknown keys, or type errors."""
    s = Settings()
    s.initFromMachineJson("config/machines/bambu_p1s.json")
    s.initFromSlicerJson("config/slicers/bambu_studio.json")
    assert _warnings(capsys) == []


@pytest.mark.parametrize("machine, expected", [
    # safe zone reaches to 110 on a 100 plate; penOffset pulls the nozzle back
    # onto it so only the one warning fires
    ({"plateSize": [100, 100], "safeZoneSize": [100, 100], "safeZoneOffset": [10, 10],
      "canvasSize": [50, 50], "canvasOffset": [10, 10], "penOffset": [10, 10]},
     "safe zone is not fully inside the plate"),
    # canvas 50..150 against a safe zone of 0..100
    ({"plateSize": [200, 200], "safeZoneSize": [100, 100], "safeZoneOffset": [0, 0],
      "canvasSize": [100, 100], "canvasOffset": [50, 50]},
     "canvas (draw zone) is not fully inside the safe zone"),
    # pen fits exactly, but the nozzle driving it sits 10mm off the plate's edge
    ({"plateSize": [100, 100], "safeZoneSize": [100, 100], "safeZoneOffset": [0, 0],
      "canvasSize": [100, 100], "canvasOffset": [0, 0], "penOffset": [10, 10]},
     "accounting for penOffset"),
], ids=["safezone-off-plate", "canvas-off-safezone", "nozzle-off-plate"])
def testOutOfBoundsGeometryWarns(tmp_path, machine, expected, capsys):
    """Each containment failure in the plate/safeZone/canvas chain is reported.

    Each case is arranged to break exactly one link, so an assertion on the
    count catches a check that has started firing for the wrong reason."""
    _load(tmp_path, {"machine": machine})
    warned = _warnings(capsys)
    assert len(warned) == 1, f"expected exactly one warning, got {warned}"
    assert expected in warned[0]


def testNestedBoundsDoNotWarn(tmp_path, capsys):
    """plate >= safe zone >= canvas, with the nozzle on the plate, is silent."""
    _load(tmp_path, {"machine": {
        "plateSize": [200, 200], "safeZoneSize": [150, 150], "safeZoneOffset": [20, 20],
        "canvasSize": [100, 100], "canvasOffset": [30, 30], "penOffset": [10, 10],
    }})
    assert _warnings(capsys) == []


@pytest.mark.parametrize("value", [0, -1, -0.5], ids=["zero", "negative", "negative-frac"])
def testNonPositiveEAxisMultiplierWarns(tmp_path, value, capsys):
    """E is elided from draw moves when it's 0, so the slicer would render the
    whole drawing as travels - worth a warning rather than a silent blank."""
    s = _load(tmp_path, {"motion": {"eAxisMultiplier": value}})
    assert s.eAxisMultiplier == value, "the value is still applied, only warned about"
    assert any("eAxisMultiplier" in w for w in _warnings(capsys))


def testPositiveEAxisMultiplierIsSilent(tmp_path, capsys):
    _load(tmp_path, {"motion": {"eAxisMultiplier": 0.5}})
    assert _warnings(capsys) == []


def testDrawHeightAboveTravelHeightWarns(tmp_path, capsys):
    """A draw height configured above the travel height would have the pen drag on
    its way between objects - lib/plot.py's _travelHeight only rescues this
    dynamically for a per-object height override, not a plain misconfigured default,
    so it's worth a warning here too."""
    s = _load(tmp_path, {"motion": {"heights": {"stroke": 5, "travel": 3}}})
    assert s.heights[LineType.STROKE] == 5, "the value is still applied, only warned about"
    warned = _warnings(capsys)
    assert any("stroke" in w and "travel" in w for w in warned)


def testDrawHeightAtOrBelowTravelHeightIsSilent(tmp_path, capsys):
    _load(tmp_path, {"motion": {"heights": {"stroke": 3, "travel": 3}}})
    assert _warnings(capsys) == []


def testBoundsHeightsAreDerivedFromDrawHeights(tmp_path):
    """The debug bounding boxes (showBoundingBoxes) always sit above every real
    draw height, one tenth of a mm apart, rather than being configured by hand."""
    s = _load(tmp_path, {"motion": {"heights": {"stroke": 1.0, "infill": 3.0, "gapInfill": 2.0, "travel": 10.0}}})
    assert s.heights[LineType._SEGMENT_BOUNDS] == pytest.approx(3.1)
    assert s.heights[LineType._PATH_BOUNDS] == pytest.approx(3.2)
    assert s.heights[LineType._DOCUMENT_BOUNDS] == pytest.approx(3.3)


def testBoundsHeightsInTheConfigAreRejected(tmp_path, capsys):
    """These three are derived, not independently settable - a leftover value in
    the JSON is reported and ignored rather than silently applied."""
    s = _load(tmp_path, {"motion": {"heights": {"stroke": 1.0, "_segmentBounds": 99.0}}})
    assert s.heights[LineType._SEGMENT_BOUNDS] == pytest.approx(1.1), "the JSON value must not win"
    assert any("_segmentBounds" in w and "computed automatically" in w for w in _warnings(capsys))


@pytest.mark.parametrize("fillSpacing", [0, -1], ids=["zero", "negative"])
def testGapInfillWithoutInfillWarns(tmp_path, fillSpacing, capsys):
    """fillSpacing <= 0 disables fill entirely, which makes gap infill a no-op."""
    _load(tmp_path, {"processing": {"fillSpacing": fillSpacing, "generateGapInfill": True}})
    assert any("generateGapInfill" in w for w in _warnings(capsys))


@pytest.mark.parametrize("processing", [
    {"fillSpacing": 0, "generateGapInfill": False},   # both off - consistent
    {"fillSpacing": 0.3, "generateGapInfill": True},  # both on - consistent
], ids=["both-off", "both-on"])
def testConsistentInfillSettingsAreSilent(tmp_path, processing, capsys):
    _load(tmp_path, {"processing": processing})
    assert _warnings(capsys) == []


def testPenNarrowerThanFillSpacingWarns(tmp_path, capsys):
    """penWidth positions the first fill/stroke ring so the pen's real edge reaches
    the true boundary - a pen narrower than fillSpacing can't actually get there."""
    s = _load(tmp_path, {"processing": {"penWidth": 0.2, "fillSpacing": 0.3}})
    assert s.penWidth == 0.2, "the value is still applied, only warned about"
    assert any("penWidth" in w for w in _warnings(capsys))


@pytest.mark.parametrize("processing", [
    {"penWidth": 0.3, "fillSpacing": 0.3},  # equal - the pen just reaches the edge
    {"penWidth": 0.5, "fillSpacing": 0.3},  # wider - the documented default relationship
    {"penWidth": 0.1, "fillSpacing": 0, "generateGapInfill": False},  # narrower, but fill is disabled - no edge to miss
], ids=["equal", "wider", "fill-disabled"])
def testPenAtLeastAsWideAsFillSpacingIsSilent(tmp_path, processing, capsys):
    _load(tmp_path, {"processing": processing})
    assert _warnings(capsys) == []


#endregion

#region bad values


@pytest.mark.parametrize("fixture, side, names", [
    ("wrong-types-machine.json", "machine",
     ("fillSpacing", "generateGapInfill", "machinePrefixFile", "unknownSetting",
      "heights", "speeds", "eAxisMultiplier", "startPos", "penOffset",
      "canvasSize", "canvasAlignment", "safeZoneAlignment", "style")),
    ("wrong-types-slicer.json", "slicer",
     ("instructionTypes", "segmentTypes")),
], ids=["machine", "slicer"])
def testWrongTypesAreAllRejected(fixture, side, names, capsys):
    """tests/configs/wrong-types-{machine,slicer}.json - every entry has a valid
    name and an invalid type, so nothing in it may be applied."""
    s = _loadFixture(fixture, side)
    _assertDefaultSettings(s, machineLoaded=side == "machine")

    # and each one has to be *reported* - silently ignoring a setting the user
    # wrote is the failure mode this whole module is trying to avoid
    warned = _warnings(capsys)
    for name in names:
        assert any(name in w for w in warned), f"{name} was skipped without a message"


def testUnknownSettingNameIsReportedNotFatal(tmp_path, capsys):
    """A typo'd key must not stop the rest of the section from loading."""
    s = _load(tmp_path, {"processing": {"filSpacing": 1.0, "fillSpacing": 0.4}})
    assert s.fillSpacing == 0.4, "a bad neighbour aborted a valid setting"
    assert any("filSpacing" in w for w in _warnings(capsys))


def testUnknownMoveTypeIsReported(tmp_path, capsys):
    """An unrecognised key inside heights/speeds/... names itself in the warning."""
    s = _load(tmp_path, {"motion": {"heights": {"stroke": 1, "sprinkle": 2}}})
    assert s.heights == {
        LineType.STROKE: 1, LineType._SEGMENT_BOUNDS: 1.1, LineType._PATH_BOUNDS: 1.2, LineType._DOCUMENT_BOUNDS: 1.3,
    }, "unknown move types must not land in the dict"
    assert any("sprinkle" in w for w in _warnings(capsys))


def testWarningsNameTheOffendingSettingsFile(tmp_path, capsys):
    """Every reported problem below the file-level checks names the config file it
    came from - the only way to tell two configs' warnings apart in a combined log."""
    path = tmp_path / "config.json"
    path.write_text('{"processing": {"filSpacing": 1.0}}', encoding="utf-8")
    Settings().initFromMachineJson(str(path))
    warned = _warnings(capsys)
    assert warned and all(str(path) in w for w in warned)


#endregion

#region conversions


def testIntIsAcceptedForFloatSetting(tmp_path):
    """JSON has no float/int distinction to speak of - `1` must mean `1.0`."""
    s = _load(tmp_path, {"processing": {"fillSpacing": 1}})
    assert s.fillSpacing == 1.0
    assert isinstance(s.fillSpacing, float)


def testFloatIsAcceptedForWholeNumberDefault(tmp_path, capsys):
    """loadDelay/maxVerticalSpeed are floats whose defaults happen to be whole
    numbers - that must not make them int-only."""
    s = _load(tmp_path, {"motion": {"loadDelay": 5.5, "maxVerticalSpeed": 12.5}})
    assert s.loadDelay == 5.5
    assert s.maxVerticalSpeed == 12.5 * 60
    assert _warnings(capsys) == []


def testSpeedsConvertToMmPerMinute(tmp_path):
    """speeds are written mm/s and stored mm/min; nothing else in the dict family is."""
    s = _load(tmp_path, {"motion": {
        "speeds": {"draw": 100, "travel": 200},
        "accels": {"draw": 100, "travel": 200},
    }})
    assert s.speeds[LineType.STROKE] == 6000
    assert s.speeds[LineType.TRAVEL] == 12000
    assert s.accels[LineType.STROKE] == 100, "accels must not be scaled"
    assert s.accels[LineType.TRAVEL] == 200


def testMaxVerticalSpeedConvertsToMmPerMinute(tmp_path):
    s = _load(tmp_path, {"machine": {"maxVerticalSpeed": 15}})
    assert s.maxVerticalSpeed == 900


def testPositionsBecomeComplex(tmp_path):
    """Every position is stored complex, matching the rest of the codebase."""
    s = _load(tmp_path, {"machine": {
        "penOffset": [1, 2], "endPos": [3, 4], "startPos": [5, 6, 7],
    }})
    assert s.penOffset == 1 + 2j
    assert s.endPos == 3 + 4j
    # startPos is the exception - it needs a Z, so it stays a dict
    assert s.startPos == {"X": 5, "Y": 6, "Z": 7}


#endregion

#region "draw" key


@pytest.mark.parametrize("name, drawValue, override, expected", [
    ("heights", 1.0, {"infill": 2.0}, (1.0, 2.0)),
    ("accels", 500, {"infill": 900}, (500, 900)),
    ("shortTravelThresholds", 0.5, {"infill": 0.1}, (0.5, 0.1)),
    ("lineTypes", "Outer wall", {"infill": "Sparse infill"}, ("Outer wall", "Sparse infill")),
])
@pytest.mark.parametrize("drawFirst", [True, False], ids=["draw-first", "override-first"])
def testDrawKeyExpandsAndIsOverridable(tmp_path, name, drawValue, override, expected, drawFirst):
    """"draw" sets all four draw roles at once; a named role wins over it.

    Ordering must not matter - _initFromJson seeds from "draw" first and then
    applies explicit keys regardless of where "draw" sits in the dict. Both
    orderings are built and serialized here so the JSON text itself differs,
    not just the source dict (dict insertion order survives json.dumps, so a
    test that only ever wrote {"draw": ..., **override} would never produce
    JSON with "draw" written second - it would look like it tests ordering
    without actually doing so)."""
    # lineTypes is the one of these four that lives on the slicer side - the
    # other three (heights/accels/shortTravelThresholds) are machine fields
    isLineTypes = name == "lineTypes"
    section = "visualization" if isLineTypes else "motion"
    side = "slicer" if isLineTypes else "machine"
    drawEntry = {"draw": drawValue}
    combined = {**drawEntry, **override} if drawFirst else {**override, **drawEntry}
    s = _load(tmp_path, {section: {name: combined}}, side)
    got = getattr(s, name)

    # heights also always carries the three derived bounding-box heights
    expectedKeys = set(DRAW_ROLES) | {LineType._SEGMENT_BOUNDS, LineType._PATH_BOUNDS, LineType._DOCUMENT_BOUNDS} if name == "heights" else set(DRAW_ROLES)
    assert set(got) == expectedKeys, f"{name} should cover exactly the draw roles"
    assert got[LineType.INFILL] == expected[1], "explicit role key did not override 'draw'"
    for role in DRAW_ROLES:
        if role != LineType.INFILL:
            assert got[role] == expected[0], f"{role} did not inherit 'draw'"


def testDrawKeyConvertsSpeedsToo(tmp_path):
    """The mm/s -> mm/min conversion applies to the expanded "draw" value as
    well as to explicit role keys - they are two separate code paths."""
    s = _load(tmp_path, {"motion": {"speeds": {"draw": 50, "infill": 25}}})
    assert s.speeds[LineType.STROKE] == 3000
    assert s.speeds[LineType.INFILL] == 1500


def testExplicitRolesWithoutDrawKey(tmp_path):
    """Without "draw", only the named roles are present - no implicit fill-in
    beyond the always-derived bounding-box heights."""
    s = _load(tmp_path, {"motion": {"heights": {"stroke": 1.0, "travel": 10.0}}})
    assert s.heights == {
        LineType.STROKE: 1.0, LineType.TRAVEL: 10.0,
        LineType._SEGMENT_BOUNDS: 1.1, LineType._PATH_BOUNDS: 1.2, LineType._DOCUMENT_BOUNDS: 1.3,
    }


def testDrawKeyDoesNotLeakIntoTravel(tmp_path):
    """"draw" is draw roles only - travel keeps whatever it was given."""
    s = _load(tmp_path, {"motion": {"heights": {"draw": 0.0, "travel": 10.0}}})
    assert s.heights[LineType.TRAVEL] == 10.0
    assert all(s.heights[r] == 0.0 for r in DRAW_ROLES)


#endregion

#region alignment


# offset (10, 5) is "distance towards the plate centre" from the named corner,
# resolved against a 50x40 canvas on a 200x200 plate. Only BL is a no-op.
@pytest.mark.parametrize("alignment, expected", [
    ("BL", 10 + 5j),
    ("BR", 140 + 5j),    # 200 - 10 - 50
    ("TL", 10 + 155j),   # 200 - 5 - 40
    ("TR", 140 + 155j),
])
def testCanvasAlignmentResolvesToLowerLeftOffset(tmp_path, alignment, expected, capsys):
    """Downstream code only ever sees a lower-left offset, so the alignment has
    to be fully resolved by the time initFromMachineJson returns."""
    s = _load(tmp_path, {"machine": {
        "plateSize": [200, 200], "safeZoneSize": [200, 200],
        "canvasSize": [50, 40], "canvasOffset": [10, 5], "canvasAlignment": alignment,
    }})
    capsys.readouterr()  # the canvas sits outside the safe zone in some cases
    assert s.canvasOffset == expected


def testAlignmentIsCaseInsensitive(tmp_path):
    s = _load(tmp_path, {"machine": {
        "plateSize": [200, 200], "safeZoneSize": [200, 200],
        "canvasSize": [50, 40], "canvasOffset": [10, 5], "canvasAlignment": "br",
    }})
    assert s.canvasAlignment == "BR"
    assert s.canvasOffset == 140 + 5j


def testSafeZoneAlignmentResolvesIndependently(tmp_path):
    """safeZone and canvas each carry their own alignment."""
    s = _load(tmp_path, {"machine": {
        "plateSize": [200, 200],
        "safeZoneSize": [100, 100], "safeZoneOffset": [10, 10], "safeZoneAlignment": "TR",
        "canvasSize": [50, 50], "canvasOffset": [20, 20], "canvasAlignment": "BL",
    }})
    assert s.safeZoneOffset == 90 + 90j  # 200 - 10 - 100
    assert s.canvasOffset == 20 + 20j


#endregion

#region machine/slicer split


def testMachineAndSlicerFieldsPartitionSettings():
    """Every Settings field must load from exactly one of the two config files -
    a field in neither would be silently unloadable from either one."""
    allFields = {f.name for f in fields(Settings())}
    assert _MACHINE_FIELDS & _SLICER_FIELDS == set(), "a field claimed by both sides"
    assert _MACHINE_FIELDS | _SLICER_FIELDS == allFields, "a field claimed by neither side"


def testSlicerFieldInMachineFileIsRejectedWithLocationHint(tmp_path, capsys):
    """A field that exists on Settings, but on the other side, is a more specific
    mistake than a typo - the warning should say which file it belongs in."""
    s = _load(tmp_path, {"visualization": {"layerChangeMessage": "; NOPE"}}, "machine")
    assert s.layerChangeMessage == "", "a misplaced setting must not be applied"
    warned = _warnings(capsys)
    assert any("layerChangeMessage" in w and "slicer" in w for w in warned)


def testMachineFieldInSlicerFileIsRejectedWithLocationHint(tmp_path, capsys):
    s = _load(tmp_path, {"processing": {"fillSpacing": 1.0}}, "slicer")
    assert s.fillSpacing == 0.3, "a misplaced setting must not be applied"
    warned = _warnings(capsys)
    assert any("fillSpacing" in w and "machine" in w for w in warned)


def testGenuineTypoIsStillReportedAsUnknown(tmp_path, capsys):
    """A name that isn't a Settings field at all (not just on the wrong side)
    keeps the plain "unknown setting" wording, with no file suggested."""
    s = _load(tmp_path, {"processing": {"filSpacing": 1.0}}, "machine")
    warned = _warnings(capsys)
    assert any("unknown setting" in w and "filSpacing" in w for w in warned)
    assert not any("belongs in" in w for w in warned)


#endregion
