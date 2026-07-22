from typing import Any, cast
from enum import Enum, auto
from dataclasses import dataclass, field, fields
import commentjson

class LineType(Enum):
    RAW_GEOMETRY = auto() # raw geometry from input file. Never drawn, so does not need height/speed/accel of its own.
    STROKE = auto()
    INFILL = auto()
    GAP_INFILL = auto()
    TRAVEL = auto()
    _SEGMENT_BOUNDS = auto()
    _PATH_BOUNDS = auto()
    _DOCUMENT_BOUNDS = auto()

# the three draw roles that "draw" (in heights/speeds/accels/lineTypes) expands to
_DRAW_LINE_TYPES = (LineType.STROKE, LineType.INFILL, LineType.GAP_INFILL)

# maps settings.json's move-type keys (heights/speeds/accels/lineTypes) to their LineType
_LINE_TYPE_KEYS = {
    "stroke": LineType.STROKE,
    "infill": LineType.INFILL,
    "gapInfill": LineType.GAP_INFILL,
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

    canvasSize: complex = 150+150j # size of the paper/drawable surface
    canvasOffset: complex = 0 # offset from origin of the canvas's lower-left corner, in pen space

    maxVerticalSpeed: float = 600 # mm/min - most printers' Z axis is slower than X/Y, so the router assumes min(speeds[travel], maxVerticalSpeed) when costing a travel's pen lift/lower

    generateStroke: bool = True # if false, strokes draw as a single centerline pass regardless of strokeWidth (the pre-multi-pass behavior) instead of expanding to multiple passes

    # motion settings
    heights: dict[LineType, float] = field(default_factory=lambda: {LineType.STROKE: 0, LineType.INFILL: 0, LineType.GAP_INFILL: 0, LineType.TRAVEL: 10})
    speeds: dict[LineType, float] = field(default_factory=lambda: {LineType.TRAVEL: 3000})
    accels: dict[LineType, float] = field(default_factory=lambda: {LineType.TRAVEL: 1000})
    shortTravelThresholds: dict[LineType, float] = field(default_factory=lambda: {LineType.STROKE: .5, LineType.INFILL: .5, LineType.GAP_INFILL: .5})
    loadDelay: float = 20

    # processing settings
    tessellationTolerance: float = .012
    fillSpacing: float = .3 # distance between concentric fill loops (mm); <= 0 disables fill
    generateGapInfill: bool = True # if true, adds extra strokes to fill small gaps in the infill

    prefixFile: str = "gcode_templates/default_prefix.gcode"
    suffixFile: str = "gcode_templates/default_suffix.gcode"

    # visualization settings
    penWidth: float = .5
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
            print("Warning: safe zone is not fully inside the plate; pen/toolhead may collide while drawing")

        canvasInSafeZone = contains(self.safeZoneOffset, self.safeZoneSize, self.canvasOffset, self.canvasSize)
        if not canvasInSafeZone:
            print("Warning: canvas (draw zone) is not fully inside the safe zone; pen/toolhead may collide while drawing")

        # the nozzle's actual gcode movement, driving the pen across the safe zone,
        # sits at safeZoneOffset - penOffset (nozzle = pen - penOffset)
        nozzleMovementInPlate = contains(0, self.plateSize, self.safeZoneOffset - self.penOffset, self.safeZoneSize)
        if not nozzleMovementInPlate:
            print("Warning: safe zone (accounting for penOffset) is not fully inside the plate; nozzle may collide while drawing")

    # warns user about invalid/inconsistent setting combinations; never resets to defaults
    def _validate(self):
        self._validateBounds()
        if self.generateGapInfill and self.fillSpacing <= 0:
            print("Warning: generateGapInfill is enabled but infill is disabled; gap infill will have no effect")

    def initFromJson(self, path):
        try:
            with open(path) as f:
                text = f.read()
        except FileNotFoundError:
            print(f"Settings file '{path}' does not exist. Using default settings.")
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
            print(f"Failed to parse settings file '{path}': {str(cause).splitlines()[0]}. Using default settings.")
            return

        if not isinstance(data, dict) or not all(isinstance(section, dict) for section in data.values()):
            print(f"Settings file '{path}' must be a JSON object of objects (sections containing settings). Using default settings.")
            return

        allowed = {f.name for f in fields(self)}
        # some settings are stored with different types than in the json
        specialTypeSettings = {"startPos", "penOffset", "plateSize", "safeZoneSize", "safeZoneOffset", "canvasSize", "canvasOffset", "endPos", "instructionTypes", "segmentTypes"}

        for sectionName, data in data.items():
            for settingName, setting in data.items():
                if settingName not in allowed:
                    print(f"Unknown setting {sectionName}.{settingName}")
                    continue

                if settingName not in specialTypeSettings:
                    expectedType = type(getattr(self, settingName))
                    if expectedType == float and type(setting) == int:
                        setting = float(setting)
                    if type(setting) != expectedType:
                        print(f"Wrong type for setting {sectionName}.{settingName}: expected {expectedType.__name__}, got {type(setting).__name__}")
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
                                print(f"Unknown move type '{k}' (reading {sectionName}.{settingName})")
                        setattr(self, settingName, temp)
                    case "penOffset" | "plateSize" | "safeZoneSize" | "safeZoneOffset" | "canvasSize" | "canvasOffset" | "endPos":
                        if not isinstance(setting, list) or len(setting) != 2:
                            print(f"Wrong type for setting {sectionName}.{settingName}: expected a 2-element list")
                            continue
                        setattr(self, settingName, complex(setting[0], setting[1]))
                    case "startPos":
                        if not isinstance(setting, list) or len(setting) != 3:
                            print(f"Wrong type for setting {sectionName}.startPos: expected a 3-element list")
                            continue
                        self.startPos = dict(zip(("X", "Y", "Z"), setting))
                    case "instructionTypes":
                        if not isinstance(setting, list) or len(setting) != 4 or not all(isinstance(v, str) for v in setting):
                            print(f"Wrong type for setting {sectionName}.instructionTypes: expected a 4-element list of strings")
                            continue
                        self.instructionTypes = tuple(setting) # type: ignore
                    case "segmentTypes":
                        if not isinstance(setting, list) or not all(isinstance(v, str) for v in setting):
                            print(f"Wrong type for setting {sectionName}.segmentTypes: expected a list of strings")
                            continue
                        self.segmentTypes = tuple(setting)
                    case "maxVerticalSpeed":
                        self.maxVerticalSpeed = setting * 60 # mm/s -> mm/min
                    case "style":
                        allowedStyles = ("role", "instruction", "segment")
                        if setting.lower() in allowedStyles:
                            self.style = setting.lower()
                        else:
                            print(f"Unknown style '{setting}' (reading {sectionName}.style)")
                    case _:
                        setattr(self, settingName, setting)
        self._validate()

        print(f"Loaded settings from file '{path}'\n")
