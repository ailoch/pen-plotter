import math, os, tempfile
from typing import TextIO

from lib.geometry import Line, Arc, PathObject, Document
from lib.settings import State, Settings

# handles gcode creation and I/O
class Plotter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.reset()

    # resets drawing state back to the configured start position.
    def reset(self):
        self.lastMoveType = ""
        self.pos = dict(self.settings.startPos) # copy - self.pos is mutated per-move, startPos must not be
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
                    match self.settings.style:
                        case "line type":
                            feature = val
                        case "instruction":
                            if 1 <= int(args["G"]) <= 3: # type: ignore
                                feature = self.settings.instructionTypes[int(args["G"]) - 1] # type: ignore
                            else:
                                feature = self.settings.instructionTypes[3]
                        case "segment":
                            segmentTypes = self.settings.segmentTypes
                            if self.lastMoveType in segmentTypes:
                                idx = (segmentTypes.index(self.lastMoveType) + 1) % len(segmentTypes)
                                feature = segmentTypes[idx]
                            else:
                                feature = segmentTypes[0]
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
            tessellated = path.tessellate(self.settings.tessellationTolerance)
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

    def createFile(self, geom: Document, fileOut: str) -> bool:
        self.reset()
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
                destFile.write("\n")

                objectCount = 0
                for object in geom.objects:
                    self.addPath(object, destFile, objectCount % 2 == 0 and self.settings.objectHeightChange)
                    objectCount += 1
                if self.settings.showBoundingBoxes:
                    self._moveRect(geom.bounds(), destFile, State._DOCUMENT_BOUNDS)

                destFile.write("\n")
                with open(self.settings.suffixFile, "r") as srcFile:
                    self.fileAppend(srcFile, destFile, replace)
            os.replace(tempPath, fileOut)
            tempPath = None
            return True
        except PermissionError as e:
            # os.replace(tempPath, fileOut) reports its src (tempPath) as e.filename
            # even when the real problem is fileOut being locked - show fileOut
            # instead so the message points at a path the user recognizes
            badFile = fileOut if e.filename == tempPath else e.filename
            print(f'Could not open file "{badFile}". Another program might be editing it.')
        except FileNotFoundError as e:
            print(f'Could not find file "{e.filename}".')
        finally:
            if tempPath and os.path.exists(tempPath):
                os.remove(tempPath)
        return False
