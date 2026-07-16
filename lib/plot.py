import math, os, tempfile
from typing import Any, cast
from enum import Enum, auto
from typing import TextIO
from dataclasses import dataclass, field, fields
import commentjson

from lib.geometry import Line, Arc, PathObject, Document

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
class PlotSettings:
    # machine settings
    startPos: dict[str, float] = field(default_factory=lambda: {"X": 0, "Y": 0, "Z": 10})
    endPos: complex = 0
    penOffset: complex = 0
    plateSize: complex = 150+150j
    drawableArea: complex = 150+150j

    # gcode settings
    heights: dict[State, float] = field(default_factory=lambda: {State.DRAW: 0, State.TRAVEL: 10})
    speeds: dict[State, float] = field(default_factory=lambda: {State.TRAVEL: 3000})
    accels: dict[State, float] = field(default_factory=lambda: {State.TRAVEL: 1000})
    shortTravelThreshold: float = .5
    tessellationTolerance: float = .012
    maxTessellationDepth: int = 10
    infillSpacing: float = .3 # distance between concentric infill loops (mm); <= 0 disables infill
    loadDelay: float = 20

    prefixFile: str = "gcode_templates/default_prefix.gcode"
    suffixFile: str = "gcode_templates/default_suffix.gcode"

    # visualization settings
    penWidth: float = .5
    lineTypes: dict[State, str] = field(default_factory=dict)
    showPenPos: bool = True
    objectHeightChange: bool = False
    style: str = "line type"
    styleLineOrder: list[str] = field(default_factory=list)

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
        specialTypeSettings = {"startPos", "penOffset", "plateSize", "drawableArea", "endPos"}

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
                    case "style":
                        allowedStyles = ("line type", "instruction", "segment")
                        if setting.lower() in allowedStyles:
                            self.style = setting.lower()
                        else:
                            print(f"Unknown style '{setting}' (reading {sectionName}.style)")
                    case _:
                        setattr(self, settingName, setting)

        print(f"Loaded settings from file '{path}'")

# handles gcode creation and I/O
class Plotter:
    def __init__(self, settingsFile: str | None = None):
        self.settings = PlotSettings()
        if settingsFile:
            self.settings.initFromJson(settingsFile)
            #TODO: check if bounds fits within plate area

        self.lastMoveType = "Custom"
        self.pos = self.settings.startPos
        self.lastSpeed = 0
        self.lastAccel = 0

    def _moveRect(self, bounds: tuple[float, float, float, float], file: TextIO, lineType: State | None = None):
        edges: tuple[complex, complex, complex, complex] = (bounds[0], bounds[1]*1j, bounds[2], bounds[3]*1j)

        self.penMove(edges[0]+edges[1], file, True)
        self.penMove(edges[0]+edges[3], file, False, lineType)
        self.penMove(edges[2]+edges[3], file, False, lineType)
        self.penMove(edges[2]+edges[1], file, False, lineType)
        self.penMove(edges[0]+edges[1], file, False, lineType)

    # adds the contents of srcFile to the end of destFile
    def fileAppend(self, srcFile: TextIO, destFile: TextIO, replace: dict[str, str | float] = {}):
        for line in srcFile:
            if "{" in line: # this saves time because the following check is much slower and most lines don't need it
                for k, v in replace.items():
                    line = line.replace("{" + k + "}", str(v))
            destFile.write(line)

    # adds a gcode line to the file with the specified arguments
    # param "accel" sets printer accel using m204 in seperate instruction
    def addLine(self, args: dict[str, str | float | None], file: TextIO, lineType: State | None = None):
        if lineType:
            args["F"] = self.settings.speeds.get(lineType)
            args["accel"] = self.settings.accels.get(lineType)
            args["type"] = self.settings.lineTypes.get(lineType)

        line = ""
        lineIsValid = False # lines must contain x, y, or z arg (g2/3 are exempt)
        for param, val in args.items():
            if not val:
                continue
            # check if param is not already set to current value
            if param != "type":
                val = float(val)
            match param:
                case "accel":
                    if val != float(self.lastAccel):
                        file.write(f"M204 S{val}\n")
                        self.lastAccel = val
                    continue
                case "type":
                    feature = ""
                    lineOrder = self.settings.styleLineOrder
                    match self.settings.style:
                        case "line type":
                            feature = val
                        case "instruction":
                            if 1 <= int(args["G"]) <= 3: # type: ignore
                                feature = lineOrder[int(args["G"]) - 1] # type: ignore
                            else:
                                feature = lineOrder[len(lineOrder) - 1]
                        case "segment":
                            if self.lastMoveType in lineOrder:
                                idx = (lineOrder.index(self.lastMoveType) + 1) % (len(lineOrder) - 1)
                                feature = lineOrder[idx]
                            else:
                                feature = lineOrder[0]
                    if feature != self.lastMoveType and "E" in args:
                        file.write(f"; FEATURE: {feature}\n")
                        self.lastMoveType = feature
                    continue
                case "F":
                    if val == self.lastSpeed:
                        continue
                    self.lastSpeed = val
                case "G":
                    if val == 2 or val == 3:
                        lineIsValid = True
                case "X" | "Y" | "Z":
                    if val == self.pos[param]:
                        continue
                    self.pos[param] = val # type: ignore
                    lineIsValid = True

            line += f"{param}{f"{val:.5f}".rstrip("0").rstrip(".")} "
        if lineIsValid:
            file.write(line.strip() + "\n")

    # moves pen to the specified location
    def penMove(self, pos: complex, file: TextIO, travel: bool = False, lineType: State | None = None, raised: bool = False):
        distSquared = (pos.real - self.pos["X"]) ** 2 + (pos.imag - self.pos["Y"]) ** 2
        if distSquared >= .000001: # moves shorter than .001 mm are probably caused by rounding errors
            if travel:
                if distSquared >= self.settings.shortTravelThreshold ** 2: # long travel
                    self.addLine({"G": "1", "Z": self.settings.heights[State.TRAVEL]}, file, State.TRAVEL)
                    self.addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file)
                    newHeight = self.settings.heights[State.DRAW]
                    if raised:
                        newHeight += .001
                    self.addLine({"G": "1", "Z": newHeight}, file)
                else: # short travel
                    self.addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file, State.DRAW)
            else: # draw moves
                newHeight = self.settings.heights[lineType or State.DRAW]
                if raised:
                    newHeight += .001
                if self.pos["Z"] != newHeight:
                    self.addLine({"G": "1", "Z": newHeight}, file)
                self.addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag), "E": math.hypot(pos.real-self.pos["X"], pos.imag-self.pos["Y"])}, file, lineType or State.DRAW)

    def addPath(self, object: PathObject, file: TextIO, raised: bool = False):
        for path in object.geometry:
            tessellated = path.tessellate(self.settings.tessellationTolerance, self.settings.maxTessellationDepth)
            for segment in tessellated.segments:
                if isinstance(segment, Line):
                    self.penMove(segment.start, file, True, raised=raised)
                    self.penMove(segment.end, file, raised=raised)
                elif isinstance(segment, Arc):
                    self.penMove(segment.point(0), file, True, raised=raised)
                    centerOffset = segment.center - segment.point(0)
                    end = segment.point(1)
                    params = {"G": "2", "X": end.real, "Y": end.imag, "I": centerOffset.real, "J": centerOffset.imag, "E": segment.length()}
                    if segment.sweep < 0:
                        params["G"] = "3"
                    self.addLine(params, file, State.DRAW)
                else:
                    print(f"Unknown path type {type(segment)}")
        if self.settings.showBoundingBoxes:
            for path in object.geometry:
                for segment in path.segments:
                    self._moveRect(segment.bounds(), file, State._SEGMENT_BOUNDS)
            self._moveRect(object.bounds(), file, State._PATH_BOUNDS)

    def createFile(self, geom: Document, fileOut: str):
        # write to a temp file in the same directory and only swap it in on success,
        # so a failure partway through won't truncate/corrupt a pre-existing fileOut
        tempPath = None
        try:
            outDir = os.path.dirname(os.path.abspath(fileOut)) or "."
            fd, tempPath = tempfile.mkstemp(dir=outDir, suffix=".tmp")
            with os.fdopen(fd, "w") as destFile:
                replace: dict[str, float | str] = {
                    "TRAVEL_HEIGHT": self.settings.heights[State.TRAVEL],
                    "TRAVEL_SPEED": self.settings.speeds[State.TRAVEL],
                    "TRAVEL_ACCEL": self.settings.accels[State.TRAVEL],
                    "LINE_WIDTH": self.settings.penWidth,
                    "LOAD_DELAY": self.settings.loadDelay,
                    "END_X": self.settings.endPos.real,
                    "END_Y": self.settings.endPos.imag
                }
                plateMaxX = self.settings.plateSize.real
                plateMaxY = self.settings.plateSize.imag
                canvasMaxX = self.settings.drawableArea.real
                canvasMaxY = self.settings.drawableArea.imag
                if self.settings.showPenPos:
                    replace["BED_EXCLUDE_AREA"] = f"0x0,{plateMaxX}x0,{plateMaxX}x{plateMaxY},{canvasMaxX}x{plateMaxY},{canvasMaxX}x{plateMaxY-canvasMaxY},0x{plateMaxY-canvasMaxY}"
                    replace["EXTRUDER_OFFSET"] = f"{self.settings.penOffset.real}x{self.settings.penOffset.imag}"
                else:
                    replace["BED_EXCLUDE_AREA"] = f"0x0,{plateMaxX}x0,{plateMaxX}x{plateMaxY-canvasMaxY},{plateMaxX-canvasMaxX}x{plateMaxY-canvasMaxY},{plateMaxX-canvasMaxX}x{plateMaxY},0x{plateMaxY}"
                    replace["EXTRUDER_OFFSET"] = "0x2" # 0x2 is the default offset

                with open(self.settings.prefixFile, "r") as srcFile:
                    self.fileAppend(srcFile, destFile, replace)

                objectCount = 0
                for object in geom.objects:
                    self.addPath(object, destFile, objectCount % 2 == 0 and self.settings.objectHeightChange)
                    objectCount += 1
                if self.settings.showBoundingBoxes:
                    self._moveRect(geom.bounds(), destFile, State._DOCUMENT_BOUNDS)

                with open(self.settings.suffixFile, "r") as srcFile:
                    self.fileAppend(srcFile, destFile, replace)
            os.replace(tempPath, fileOut)
            tempPath = None
            print("Post process completed successfully")
        except PermissionError as e:
            print(f'Could not open file "{e.filename}". Another program might be editing it.')
        except FileNotFoundError as e:
            print(f'Could not find file "{e.filename}".')
        finally:
            if tempPath and os.path.exists(tempPath):
                os.remove(tempPath)
