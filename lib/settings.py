from typing import Any, cast
from enum import Enum, auto
from dataclasses import dataclass, field, fields
import commentjson

class State(Enum):
    DRAW = auto()
    TRAVEL = auto()
    _SEGMENT_BOUNDS = auto()
    _PATH_BOUNDS = auto()
    _DOCUMENT_BOUNDS = auto()

# maps settings.json's move-type keys (heights/speeds/accels/lineTypes) to their State
_STATE_KEYS = {
    "draw": State.DRAW,
    "travel": State.TRAVEL,
    "_segmentBounds": State._SEGMENT_BOUNDS,
    "_pathBounds": State._PATH_BOUNDS,
    "_documentBounds": State._DOCUMENT_BOUNDS,
}

@dataclass
class Settings:
    # machine settings
    startPos: dict[str, float] = field(default_factory=lambda: {"X": 0, "Y": 0, "Z": 10})
    endPos: complex = 0
    penOffset: complex = 0
    plateSize: complex = 150+150j
    drawableArea: complex = 150+150j

    # motion settings
    heights: dict[State, float] = field(default_factory=lambda: {State.DRAW: 0, State.TRAVEL: 10})
    speeds: dict[State, float] = field(default_factory=lambda: {State.TRAVEL: 3000})
    accels: dict[State, float] = field(default_factory=lambda: {State.TRAVEL: 1000})
    shortTravelThreshold: float = .5
    loadDelay: float = 20

    # processing settings
    tessellationTolerance: float = .012
    infillSpacing: float = .3 # distance between concentric infill loops (mm); <= 0 disables infill

    prefixFile: str = "gcode_templates/default_prefix.gcode"
    suffixFile: str = "gcode_templates/default_suffix.gcode"

    # visualization settings
    penWidth: float = .5
    showPenPos: bool = True
    objectHeightChange: bool = False

    style: str = "line type" # options are "line type", "instruction", and "segment"
    lineTypes: dict[State, str] = field(default_factory=dict) # used when style is "line type"
    instructionTypes: tuple[str, str, str, str] = ("Outer wall", "Overhang wall", "Support interface", "Gap infill") # used when style is "instruction" - index 0 is G0/G1, 1 is G2, 2 is G3, 3 is everything else
    segmentTypes: tuple[str, ...] = field(default_factory=lambda: ("Sparse infill", "Support interface", "Overhang wall", "Internal solid infill", "Gap infill")) # used when style is "segment" - each instruction cycles to the next entry

    # debug settings
    showBoundingBoxes: bool = False
    optimizePathOrder: bool = True
    profiling: bool = False # if true, profiles _Process.py's pipeline and prints the slowest functions

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

        allowed = {f.name for f in fields(self)}
        # some settings are stored with different types than in the json
        specialTypeSettings = {"startPos", "penOffset", "plateSize", "drawableArea", "endPos", "instructionTypes", "segmentTypes"}

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
                    case "heights" | "speeds" | "accels" | "lineTypes":
                        temp = {}
                        for k, v in setting.items():
                            if k in _STATE_KEYS:
                                # speeds needs to be converted mm/min -> mm/s
                                temp[_STATE_KEYS[k]] = v*60 if settingName == "speeds" else v
                            else:
                                print(f"Unknown move type '{k}' (reading {sectionName}.{settingName})")
                        setattr(self, settingName, temp)
                    case "penOffset" | "plateSize" | "drawableArea" | "endPos":
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
                    case "style":
                        allowedStyles = ("line type", "instruction", "segment")
                        if setting.lower() in allowedStyles:
                            self.style = setting.lower()
                        else:
                            print(f"Unknown style '{setting}' (reading {sectionName}.style)")
                    case _:
                        setattr(self, settingName, setting)

        print(f"Loaded settings from file '{path}'")
