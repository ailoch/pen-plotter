from typing import Any, cast
from enum import Enum, auto
from dataclasses import dataclass, field, fields
import commentjson

class LineType(Enum):
    RAW_GEOMETRY = auto() # raw geometry from input file. Never drawn, so does not need height/speed/accel of its own.
    STROKE = auto()
    INFILL = auto()
    GAP_INFILL = auto()
    INVALID = auto() # a draw segment that falls outside the canvas - only used when settings.showOutOfBounds is true
    TRAVEL = auto()
    _SEGMENT_BOUNDS = auto()
    _PATH_BOUNDS = auto()
    _DOCUMENT_BOUNDS = auto()

# the draw roles that "draw" (in heights/speeds/accels/lineTypes) expands to
_DRAW_LINE_TYPES = (LineType.STROKE, LineType.INFILL, LineType.GAP_INFILL, LineType.INVALID)

# valid safeZoneAlignment/canvasAlignment values - first char is vertical (Bottom/Top),
# second is horizontal (Left/Right), naming the plate corner the *Offset is measured from
_ALIGNMENTS = ("BL", "BR", "TL", "TR")

# converts an alignment + "distance towards center from that corner" offset into the
# equivalent lower-left-corner offset (the representation the rest of the pipeline uses,
# and what a "BL" alignment already *is* - so BL is a no-op here, preserving old behavior)
def _alignedOffset(alignment: str, offset: complex, rectSize: complex, containerSize: complex) -> complex:
    left = alignment[1] == "L"
    bottom = alignment[0] == "B"
    x = offset.real if left else containerSize.real - offset.real - rectSize.real
    y = offset.imag if bottom else containerSize.imag - offset.imag - rectSize.imag
    return complex(x, y)

# maps settings.json's move-type keys (heights/speeds/accels/lineTypes) to their LineType
_LINE_TYPE_KEYS = {
    "stroke": LineType.STROKE,
    "infill": LineType.INFILL,
    "gapInfill": LineType.GAP_INFILL,
    "invalid": LineType.INVALID,
    "travel": LineType.TRAVEL,
    "_segmentBounds": LineType._SEGMENT_BOUNDS,
    "_pathBounds": LineType._PATH_BOUNDS,
    "_documentBounds": LineType._DOCUMENT_BOUNDS,
}

@dataclass
class Settings:
    # machine settings
    startPos: dict[str, float] = field(default_factory=lambda: {"X": 0, "Y": 0, "Z": 10})
    endPos: complex = 0
    penOffset: complex = 0
    plateSize: complex = 150+150j # plate rect size; lower-left corner fixed at origin
    safeZoneSize: complex = 150+150j # size of the area the pen can reach without colliding
    safeZoneOffset: complex = 0 # offset from origin of the safe zone's lower-left corner, in pen space
    safeZoneAlignment: str = "BL" # which plate corner safeZoneOffset is measured from ("BL"/"BR"/"TL"/"TR")

    canvasSize: complex = 150+150j # size of the paper/drawable surface
    canvasOffset: complex = 0 # offset from origin of the canvas's lower-left corner, in pen space
    canvasAlignment: str = "BL" # which plate corner canvasOffset is measured from ("BL"/"BR"/"TL"/"TR")

    # deliberately a float - the parsing logic would assume float values are invalid if this was an int
    maxVerticalSpeed: float = 600.0 # mm/min - most printers' Z axis is slower than X/Y, so the router assumes min(speeds[travel], maxVerticalSpeed) when costing a travel's pen lift/lower

    # motion settings
    heights: dict[LineType, float] = field(default_factory=lambda: {LineType.STROKE: 0, LineType.INFILL: 0, LineType.GAP_INFILL: 0, LineType.INVALID: 0, LineType.TRAVEL: 10})
    speeds: dict[LineType, float] = field(default_factory=lambda: {LineType.TRAVEL: 3000})
    accels: dict[LineType, float] = field(default_factory=lambda: {LineType.TRAVEL: 1000})
    shortTravelThresholds: dict[LineType, float] = field(default_factory=lambda: {LineType.STROKE: .5, LineType.INFILL: .5, LineType.GAP_INFILL: .5})
    loadDelay: float = 20.0
    showLoadProgress: bool = True # if true, {WAIT_FOR_PEN} counts the load delay down on the printer's progress display (M73) instead of dwelling silently
    eAxisMultiplier: float = 1.0 # scales every emitted E value - reduces the raw commanded E rate the P1S planner throttles XY speed against; side effect: the slicer's total-filament stat scales down by the same factor

    # processing settings
    penWidth: float = .5 # expected ink width (mm)
    fillSpacing: float = .3 # max distance (mm) between adjacent passes/loops before white paper shows through; <= 0 disables fill
    generateGapInfill: bool = True # if true, adds extra strokes to fill small gaps in the infill
    generateStroke: bool = True # if false, strokes draw as a single centerline pass regardless of strokeWidth (the pre-multi-pass behavior) instead of expanding to multiple passes
    tessellationTolerance: float = .012
    showOutOfBounds: bool = False # if true, segments outside the canvas are still drawn, tagged LineType.INVALID for slicer-preview visibility; if false, they're cropped to the canvas edge

    prefixFile: str = "gcode_templates/default_prefix.gcode"
    suffixFile: str = "gcode_templates/default_suffix.gcode"

    # visualization settings
    showPenPos: bool = True
    objectHeightChange: bool = False

    styleChangeMessage: str = "" # printed before a line whose feature (draw role) changes; %s is replaced with the feature name
    layerChangeMessage: str = "" # printed between objects when objectHeightChange is true

    style: str = "role" # options are "role", "instruction", and "segment"
    lineTypes: dict[LineType, str] = field(default_factory=dict) # used when style is "role"
    instructionTypes: tuple[str, str, str, str] = ("Outer wall", "Overhang wall", "Support interface", "Gap infill") # used when style is "instruction" - index 0 is G0/G1, 1 is G2, 2 is G3, 3 is everything else
    segmentTypes: tuple[str, ...] = field(default_factory=lambda: ("Sparse infill", "Support interface", "Overhang wall", "Internal solid infill", "Gap infill")) # used when style is "segment" - each instruction cycles to the next entry

    # debug settings
    showBoundingBoxes: bool = False
    optimizePathOrder: bool = True
    profiling: bool = False # if true, profiles _Process.py's pipeline and prints the slowest functions

    # warns user if plate, safe zone, and canvas are not aligned properly
    # also considers penOffset
    def _validateBounds(self):
        def contains(outerOffset: complex, outerSize: complex, innerOffset: complex, innerSize: complex, epsilon: float = 1e-6) -> bool:
            return (
                innerOffset.real >= outerOffset.real - epsilon and
                innerOffset.imag >= outerOffset.imag - epsilon and
                innerOffset.real + innerSize.real <= outerOffset.real + outerSize.real + epsilon and
                innerOffset.imag + innerSize.imag <= outerOffset.imag + outerSize.imag + epsilon
            )

        # safeZoneOffset is already in pen space, i.e. already expressed in the same
        # physical bed-frame numbers the plate rect uses, so this is a direct compare
        safeZoneInPlate = contains(0, self.plateSize, self.safeZoneOffset, self.safeZoneSize)
        if not safeZoneInPlate:
            print("Warning: safe zone is not fully inside the plate; pen/toolhead may collide while drawing.")

        canvasInSafeZone = contains(self.safeZoneOffset, self.safeZoneSize, self.canvasOffset, self.canvasSize)
        if not canvasInSafeZone:
            print("Warning: canvas (draw zone) is not fully inside the safe zone; pen/toolhead may collide while drawing.")

        # the nozzle's actual gcode movement, driving the pen across the safe zone,
        # sits at safeZoneOffset - penOffset (nozzle = pen - penOffset)
        nozzleMovementInPlate = contains(0, self.plateSize, self.safeZoneOffset - self.penOffset, self.safeZoneSize)
        if not nozzleMovementInPlate:
            print("Warning: safe zone (accounting for penOffset) is not fully inside the plate; nozzle may collide while drawing.")

    # warns user about invalid/inconsistent setting combinations; never resets to defaults
    def _validate(self):
        self._validateBounds()
        if self.generateGapInfill and self.fillSpacing <= 0:
            print("Warning: generateGapInfill is enabled but infill is disabled; gap infill will have no effect.")
        if self.fillSpacing > 0 and self.penWidth < self.fillSpacing:
            print("Warning: penWidth is narrower than fillSpacing; the pen may not actually reach the edge of filled/stroked shapes.")
        if self.eAxisMultiplier <= 0:
            print("Warning: eAxisMultiplier <= 0 drops the E value from every draw move; the slicer will render the whole drawing as travel moves.")

    def initFromJson(self, path):
        try:
            with open(path) as f:
                text = f.read()
        except FileNotFoundError:
            print(f"Warning: settings file '{path}' does not exist; using default settings.")
            return

        try:
            data = commentjson.loads(text)
        except Exception as e:
            # remove a traceback from the error message
            # this makes the error much more readable
            cause = e.__context__ or e

            # a ValueError is thrown when the input can't be tokenized
            # the error contains the entire source text, so we need to figure out the exact cause of the error
            if isinstance(cause, ValueError) and cause.args[:1] == ("Unable to parse text",):
                try:
                    commentjson.commentjson.parser.parse(text)
                except Exception as parseError:
                    cause = parseError
            print(f"Warning: failed to parse settings file '{path}' ({str(cause).splitlines()[0]}); using default settings.")
            return

        if not isinstance(data, dict) or not all(isinstance(section, dict) for section in data.values()):
            print(f"Warning: settings file '{path}' must be a JSON object of objects (sections containing settings); using default settings.")
            return

        allowed = {f.name for f in fields(self)}
        # some settings are stored with different types than in the json
        specialTypeSettings = {"startPos", "penOffset", "plateSize", "safeZoneSize", "safeZoneOffset", "canvasSize", "canvasOffset", "endPos", "instructionTypes", "segmentTypes"}

        for sectionName, data in data.items():
            for settingName, setting in data.items():
                if settingName not in allowed:
                    print(f"Warning: unknown setting '{settingName}' found while reading {sectionName} in '{path}'.")
                    continue

                if settingName not in specialTypeSettings:
                    expectedType = type(getattr(self, settingName))
                    if expectedType == float and type(setting) == int:
                        setting = float(setting)
                    if type(setting) != expectedType:
                        print(f"Warning: wrong type for setting {sectionName}.{settingName} in '{path}'; expected {expectedType.__name__}, got {type(setting).__name__}.")
                        continue
                setting = cast(Any, setting)

                match settingName: # some properties need special logic
                    case "heights" | "speeds" | "accels" | "lineTypes" | "shortTravelThresholds":
                        temp = {}
                        # "draw" sets all three draw roles (stroke/infill/gapInfill) at
                        # once; an explicit role key below overrides it for that role
                        if "draw" in setting:
                            v = setting["draw"]
                            v = v*60 if settingName == "speeds" else v
                            for lt in _DRAW_LINE_TYPES:
                                temp[lt] = v
                        for k, v in setting.items():
                            if k == "draw":
                                continue
                            if k in _LINE_TYPE_KEYS:
                                # speeds needs to be converted mm/min -> mm/s
                                temp[_LINE_TYPE_KEYS[k]] = v*60 if settingName == "speeds" else v
                            else:
                                print(f"Warning: unknown move type '{k}' found while reading {sectionName}.{settingName} in '{path}'.")
                        setattr(self, settingName, temp)
                    case "penOffset" | "plateSize" | "safeZoneSize" | "safeZoneOffset" | "canvasSize" | "canvasOffset" | "endPos":
                        if not isinstance(setting, list) or len(setting) != 2:
                            print(f"Warning: wrong type for setting {sectionName}.{settingName} in '{path}'; expected a 2-element list.")
                            continue
                        setattr(self, settingName, complex(setting[0], setting[1]))
                    case "startPos":
                        if not isinstance(setting, list) or len(setting) != 3:
                            print(f"Warning: wrong type for setting {sectionName}.startPos in '{path}'; expected a 3-element list.")
                            continue
                        self.startPos = dict(zip(("X", "Y", "Z"), setting))
                    case "instructionTypes":
                        if not isinstance(setting, list) or len(setting) != 4 or not all(isinstance(v, str) for v in setting):
                            print(f"Warning: wrong type for setting {sectionName}.instructionTypes in '{path}'; expected a 4-element list of strings.")
                            continue
                        self.instructionTypes = tuple(setting) # type: ignore
                    case "segmentTypes":
                        if not isinstance(setting, list) or not all(isinstance(v, str) for v in setting):
                            print(f"Warning: wrong type for setting {sectionName}.segmentTypes in '{path}'; expected a list of strings.")
                            continue
                        self.segmentTypes = tuple(setting)
                    case "maxVerticalSpeed":
                        self.maxVerticalSpeed = setting * 60 # mm/s -> mm/min
                    case "safeZoneAlignment" | "canvasAlignment":
                        if setting.upper() in _ALIGNMENTS:
                            setattr(self, settingName, setting.upper())
                        else:
                            print(f"Warning: unknown alignment '{setting}' found while reading {sectionName}.{settingName} in '{path}'; expected one of {_ALIGNMENTS}.")
                    case "style":
                        allowedStyles = ("role", "instruction", "segment")
                        if setting.lower() in allowedStyles:
                            self.style = setting.lower()
                        else:
                            print(f"Warning: unknown style type '{setting}' found while reading {sectionName}.style in '{path}'.")
                    case _:
                        setattr(self, settingName, setting)

        # safeZoneOffset/canvasOffset are read as "distance towards center from the
        # aligned plate corner" - convert to the lower-left-corner offset the rest of
        # the pipeline expects, now that both the offset and alignment are known
        self.safeZoneOffset = _alignedOffset(self.safeZoneAlignment, self.safeZoneOffset, self.safeZoneSize, self.plateSize)
        self.canvasOffset = _alignedOffset(self.canvasAlignment, self.canvasOffset, self.canvasSize, self.plateSize)

        self._validate()

        print(f"Loaded settings from file '{path}'\n")
