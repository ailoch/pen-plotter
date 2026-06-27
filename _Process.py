import math
from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import TextIO, Self
from dataclasses import dataclass, field
from scipy.integrate import quad
import svgelements

class State(Enum):
    DRAW = auto()
    TRAVEL = auto()
    _SEGMENT_BOUNDS = auto()
    _PATH_BOUNDS = auto()
    _DOCUMENT_BOUNDS = auto()

# plot settings
#TODO: move settings to json
fileIn = "testDrawing.svg" # hardcoded to speed up testing, need to ask user later
fileOut = "testDrawing.gcode"
prefixFile = "ーstartCode.gcode"
suffixFile = "ーendCode.gcode"

#region shapeDefs

# stores an object style (line width, color, fill)
@dataclass
class Style:
    strokeWidth: float = 1
    strokeColor: list[int] = field(default_factory=lambda: [0, 0, 0])
    fillColor: list[int] = field(default_factory=lambda: [0, 0, 0])

# stores an affine transformation (rotation, scaling, shear, transform)
class Transform:
    def __init__(self, matrix: list[float] | None = None):
        if matrix:
            self.matrix = matrix
        else:
            self.matrix = [1, 0, 0, 1, 0, 0]

    def __repr__(self):
        return f"Transform(matrix={self.matrix!r})"

    def __matmul__(self, other: list[float] | Self):
        if isinstance(other, list):
            return Transform(self._getTransform(other))
        else:
            return Transform(self._getTransform(other.matrix))

    def __imatmul__(self, other: list[float] | Self):
        if isinstance(other, list):
            return Transform(self._getTransform(other))
        else:
            return Transform(self._getTransform(other.matrix))

    def __mul__(self, other: list[float] | Self):
        if isinstance(other, list):
            return Transform(self._getReverseTransform(other))
        else:
            return Transform(self._getReverseTransform(other.matrix))

    def __imul__(self, other: list[float] | Self):
        if isinstance(other, list):
            return Transform(self._getReverseTransform(other))
        else:
            return Transform(self._getReverseTransform(other.matrix))

    def _getTransform(self, other: list[float]):
        return [
            self.matrix[0]*other[0] + self.matrix[2]*other[1],
            self.matrix[1]*other[0] + self.matrix[3]*other[1],
            self.matrix[0]*other[2] + self.matrix[2]*other[3],
            self.matrix[1]*other[2] + self.matrix[3]*other[3],
            self.matrix[0]*other[4] + self.matrix[2]*other[5] + self.matrix[4],
            self.matrix[1]*other[4] + self.matrix[3]*other[5] + self.matrix[5]
        ]

    # returnss other@self instrad of self@other
    def _getReverseTransform(self, other: list[float]):
        return [
            other[0]*self.matrix[0] + other[2]*self.matrix[1],
            other[1]*self.matrix[0] + other[3]*self.matrix[1],
            other[0]*self.matrix[2] + other[2]*self.matrix[3],
            other[1]*self.matrix[2] + other[3]*self.matrix[3],
            other[0]*self.matrix[4] + other[2]*self.matrix[5] + other[4],
            other[1]*self.matrix[4] + other[3]*self.matrix[5] + other[5]
        ]

    def apply(self, p: complex) -> complex:
        x, y = p.real, p.imag

        return complex(
            self.matrix[0]*x + self.matrix[2]*y + self.matrix[4],
            self.matrix[1]*x + self.matrix[3]*y + self.matrix[5]
        )

    # same as apply(), but ignores translations
    def applyVector(self, v: complex) -> complex:
        x, y = v.real, v.imag

        return complex(
            self.matrix[0]*x + self.matrix[2]*y,
            self.matrix[1]*x + self.matrix[3]*y
        )

    def translate(self, x: float, y: float | None = None):
        if not y:
            y = x
        self.matrix = self._getReverseTransform([1, 0, 0, 1, x, y])

    def scale(self, sx: float, sy: float | None = None):
        if not sy:
            sy = sx
        self.matrix = self._getReverseTransform([sx, 0, 0, sy, 0, 0])

    def rotate(self, angle: float, cx: float = 0, cy: float = 0):
        rot = [math.cos(math.radians(angle)), math.sin(math.radians(angle)), -math.sin(math.radians(angle)), math.cos(math.radians(angle)), 0, 0]
        if cx == 0 and cy == 0:
            self.matrix = self._getReverseTransform(rot)
        else:
            self.matrix = self._getReverseTransform([1, 0, 0, 1, cx, cy])
            self.matrix = self._getReverseTransform(rot)
            self.matrix = self._getReverseTransform([1, 0, 0, 1, -cx, -cy])

    def skewX(self, angle: float):
        self.matrix = self._getReverseTransform([1, 0, math.tan(math.radians(angle)), 1, 0, 0])

    def skewY(self, angle: float):
        self.matrix = self._getReverseTransform([1, math.tan(math.radians(angle)), 0, 1, 0, 0])

    def flipAlongX(self):
        self.matrix = self._getReverseTransform([-1, 0, 0, 1, 0, 0])

    def flipAlongY(self):
        self.matrix = self._getReverseTransform([1, 0, 0, -1, 0, 0])

# wrapper for different types of path segments
class Segment(ABC):
    @abstractmethod
    def length(self) -> float:
        """Return the arc length"""

    @abstractmethod
    def point(self, t: float) -> complex:
        """Return the point at t (0 <= t <= 1)"""

    @abstractmethod
    def applyTransform(self, t: Transform):
        """Apply an affine transformation"""

    @abstractmethod
    def reverse(self):
        """Reverse the segment direction"""

    @abstractmethod
    def derivative(self, t: float) -> complex:
        """Return the derivative at point t (0 <= t <= 1)"""

    @abstractmethod
    def extrema(self) -> list[float]:
        """Return the x and y extrema of a segment"""

    # return (xmin, ymin, xmax, ymax)
    def bounds(self) -> tuple[float, float, float, float]:
        candidates: list[float] = [0, 1] # start/end of segment
        candidates.extend(self.extrema())

        pts = [self.point(t) for t in candidates]

        xs = [p.real for p in pts]
        ys = [p.imag for p in pts]

        temp = (min(xs), min(ys), max(xs), max(ys))

        # bambu studio renderer breaks if very large cordinates are given
        return (max(temp[0], -5000), max(temp[1], -5000), min(temp[2], 5256), min(temp[3], 5256))

@dataclass
class Line(Segment):
    start: complex = complex()
    end: complex = complex()

    def length(self) -> float:
        d = self.start - self.end
        return math.hypot(d.real, d.imag)

    def point(self, t: float) -> complex:
        return t * (self.end-self.start) + self.start

    def applyTransform(self, t: Transform):
        self.start = t.apply(self.start)
        self.end = t.apply(self.end)

    def reverse(self):
        self.end, self.start = self.start, self.end

    def derivative(self, t: float) -> complex:
        # line derivative is constant, so t is irrelavent
        return self.end - self.start

    def extrema(self) -> list[float]:
        # lines have no extrema
        return []

@dataclass
class Arc(Segment):
    center: complex = 0
    u: complex = 0
    v: complex = 0
    t0: float = 0
    sweep: float = 2*math.pi # sweep is between -2pi (ccw) and 2pi (cw)

    def _containsAngle(self, theta: float) -> bool:
        return self._thetaToT(theta) is not None

    def _pointAtAngle(self, theta: float) -> complex:
        return self.center + self.u*math.cos(theta) + self.v*math.sin(theta)

    def _thetaToT(self, theta: float) -> float | None:
        for k in range(-2, 3):
            candidate = theta + k*math.tau
            t = (candidate-self.t0) / self.sweep

            if 0 <= t <= 1:
                return t
        return None

    def length(self) -> float:
        def speed(t):
            return abs(self.derivative(t))
        length, _ = quad(speed, 0, 1)
        return abs(length)

    def point(self, t: float) -> complex:
        theta = self.t0 + t*self.sweep
        return self._pointAtAngle(theta)

    def applyTransform(self, t: Transform):
        self.center = t.apply(self.center)
        self.u = t.applyVector(self.u)
        self.v = t.applyVector(self.v)

    def reverse(self):
        self.t0 += self.sweep
        self.sweep = -self.sweep

    def derivative(self, t: float) -> complex:
        theta = self.t0 + t*self.sweep
        return self.derivativeFromTheta(theta) * self.sweep

    def derivativeFromTheta(self, theta: float) -> complex:
        return -self.u*math.sin(theta) + self.v*math.cos(theta)

    def extrema(self) -> list[float]:
        extrema = []

        for theta in (
            math.atan2(self.v.real, self.u.real),
            math.atan2(self.v.real, self.u.real) + math.pi,
            math.atan2(self.v.imag, self.u.imag),
            math.atan2(self.v.imag, self.u.imag) + math.pi
        ):
            t = self._thetaToT(theta)
            if t is not None:
                extrema.append(t)
        return extrema

@dataclass
class QuadraticBezier(Segment):
    start: complex = 0
    p1: complex = 0
    end: complex = 0

    def _axisExtrema(self, p0: float, p1: float, p2: float) -> list[float]:
        denom = p0 - 2*p1 + p2
        if abs(denom) < 1e-12:
            return []

        t = (p0-p1) / denom
        if 0 < t < 1:
            return [t]

        return []

    def length(self) -> float:
        def speed(t):
            return abs(self.derivative(t))
        length, _ = quad(speed, 0, 1)
        return length

    def point(self, t: float) -> complex:
        return (
            self.start * (1-t) ** 2 +
            self.p1 * t * 2 * (1-t) +
            self.end * t**2
        )

    def applyTransform(self, t: Transform):
        self.start = t.apply(self.start)
        self.p1 = t.apply(self.p1)
        self.end = t.apply(self.end)

    def reverse(self):
        self.start, self.end = self.end, self.start

    def derivative(self, t: float) -> complex:
        return (
            2 * (1-t) * (self.p1-self.start) +
            2 * t * (self.end-self.p1)
        )

    def extrema(self) -> list[float]:
        ts = self._axisExtrema(self.start.real, self.p1.real, self.end.real)
        ts += self._axisExtrema(self.start.imag, self.p1.imag, self.end.imag)
        return ts

@dataclass
class CubicBezier(Segment):
    start: complex = 0
    p1: complex = 0
    p2: complex = 0
    end: complex = 0

    def length(self) -> float:
        def speed(t):
            return abs(self.derivative(t))
        length, _ = quad(speed, 0, 1)
        return length

    def _axisExtrema(self, a: float, b: float, c: float) -> list[float]:
        if abs(a) < 1e-12: # prevent division by 0
            return []
        disc = b*b - 4*a*c
        ts = []
        if disc >= 0:
            s = math.sqrt(disc)

            t1 = (-b+s) / (2*a)
            if 0 < t1 < 1:
                ts.append(t1)

            t2 = (-b-s) / (2*a)
            if 0 < t2 < 1:
                ts.append(t2)
        return ts

    def point(self, t: float) -> complex:
        return (
            self.start * (1-t) ** 3 +
            self.p1 * t * 3 * (1-t) ** 2 +
            self.p2 * t**2 * 3 * (1-t) +
            self.end * t**3
        )

    def applyTransform(self, t: Transform):
        self.start = t.apply(self.start)
        self.p1 = t.apply(self.p1)
        self.p2 = t.apply(self.p2)
        self.end = t.apply(self.end)

    def reverse(self):
        self.start, self.end = self.end, self.start
        self.p1, self.p2 = self.p2, self.p1

    def derivative(self, t: float) -> complex:
        return (
            3 * (1-t) ** 2 * (self.p1-self.start) +
            6 * (1-t) * t * (self.p2-self.p1) +
            3 * t**2 * (self.end-self.p2)
        )

    def extrema(self) -> list[float]:
        a = -self.start + 3*self.p1 - 3*self.p2 + self.end
        b = 3*self.start - 6*self.p1 + 3*self.p2
        c = -3*self.start + 3*self.p1

        ts = self._axisExtrema(3*a.real, 2*b.real, c.real)
        ts += self._axisExtrema(3*a.imag, 2*b.imag, c.imag)
        return ts

# stores a list of segments
@dataclass
class Path:
    segments: list[Segment] = field(default_factory=list)

    def length(self) -> float:
        len = 0
        for segment in self.segments:
            len += segment.length()
        return len

    def reverse(self):
        self.segments.reverse()
        for segment in self.segments:
            segment.reverse()

    def bounds(self) -> tuple[float, float, float, float]:
        bounds = (math.inf, math.inf, -math.inf, -math.inf)
        for segment in self.segments:
            segmentBounds = segment.bounds()
            bounds = (min(bounds[0], segmentBounds[0]), min(bounds[1], segmentBounds[1]), max(bounds[2], segmentBounds[2]), max(bounds[3], segmentBounds[3]))
        return bounds

    def tessellate(self): #TODO
        pass

# stores a path, style, and transform
@dataclass
class PathObject:
    id: str
    geometry: Path = field(default_factory=Path)
    style: Style = field(default_factory=Style)
    transform : Transform = field(default_factory=Transform)

    def __iadd__(self, segment):
        self.geometry.segments.append(segment)
        return self

    def applyTransformations(self):
        for segment in self.geometry.segments:
            segment.applyTransform(self.transform)
        self.transform = Transform() # reset transformation

# overall document
class Document:
    def __init__(self):
        self.objects: list[PathObject] = []
        self.id: dict[str, PathObject] = {}

    def __repr__(self):
        return f"Document(id={self.id!r})"

    def add(self, obj: PathObject): #FIXME: adding an object with id that already exists will break the relation between objects and id
        self.objects.append(obj)
        if obj.id is not None:
            self.id[obj.id] = obj

    def bounds(self) -> tuple[float, float, float, float]:
        bounds = (math.inf, math.inf, -math.inf, -math.inf)
        for object in self.objects:
            segmentBounds = object.geometry.bounds()
            bounds = (min(bounds[0], segmentBounds[0]), min(bounds[1], segmentBounds[1]), max(bounds[2], segmentBounds[2]), max(bounds[3], segmentBounds[3]))
        return bounds

#endregion shapeDefs

# handles gcode creation and I/O
class Plotter:
    def __init__(self):
        self.pos: dict[str, float] = {"X": 128, "Y": 128, "Z": 10} # initial pen position
        self.heights = {State.DRAW: 1, State.TRAVEL: 5, State._SEGMENT_BOUNDS: 2, State._PATH_BOUNDS: 3, State._DOCUMENT_BOUNDS: 4} # pen heights in mm
        self.speeds = {State.DRAW: 1800, State.TRAVEL: 6000} # mm/min
        self.accels = {State.DRAW: 3000, State.TRAVEL: 10000} # mm/s^2
        self.lineTypes = {State.DRAW: "Outer wall", State._SEGMENT_BOUNDS: "Support transition", State._PATH_BOUNDS: "Support interface", State._DOCUMENT_BOUNDS: "Support"}
        self.width = .7 # pen width in mm
        self.offset: tuple[float, float] = (-40, 5) # pen offset from extruder

        self.loadDelay = 20 # delay (seconds) printer waits while pen is loaded
        self.shortTravelThreshold = .7 # travels below this distance will not lift the pen
        self.avgTesselatedLineLength = .5 # average length per line on tesselated paths in mm
        self.drawableArea = (215.9, 230) #TODO: implement bounds checking
        self.showPenPos = True # if false, nozzle position will be shown in slicer

        self.showBB = False

        self.lastMoveType = "Custom"
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
    def fileAppend(self, srcFile: TextIO, destFile: TextIO, replace: dict[str, str] = {}):
        for line in srcFile:
            if "{" in line: # this saves time because the following check is much slower and most lines don't need it
                for k, v in replace.items():
                    line = line.replace("{" + k + "}", str(v))
            destFile.write(line)

    # adds a gcode line to the file with the specified arguments
    # param "A" sets printer accel using m204 in seperate instruction
    def addLine(self, args: dict[str, str | float | None], file: TextIO, lineType: State | None = None):
        if lineType:
            args["F"] = self.speeds.get(lineType)
            args["accel"] = self.accels.get(lineType)
            args["type"] = self.lineTypes.get(lineType)

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
                    if val != self.lastMoveType and "E" in args:
                        file.write(f"; FEATURE: {val}\n")
                        self.lastMoveType = val
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
    def penMove(self, pos: complex, file: TextIO, travel: bool = False, lineType: State | None = None):
        distSquared = (pos.real - self.pos["X"]) ** 2 + (pos.imag - self.pos["Y"]) ** 2
        if distSquared >= .000001: # moves shorter than .001 mm are probably caused by rounding errors
            if travel:
                if distSquared >= self.shortTravelThreshold ** 2: # long travel
                    self.addLine({"G": "1", "Z": self.heights[State.TRAVEL]}, file, State.TRAVEL)
                    self.addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file)
                    self.addLine({"G": "1", "Z": self.heights[State.DRAW]}, file)
                else: # short travel
                    self.addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file, State.DRAW)
            else: # draw moves
                if self.pos["Z"] != self.heights[lineType or State.DRAW]:
                    self.addLine({"G": "1", "Z": self.heights[lineType or State.DRAW]}, file)
                self.addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag), "E": math.hypot(pos.real-self.pos["X"], pos.imag-self.pos["Y"])}, file, lineType or State.DRAW)

    def tesselate(self, segment: Segment): #TODO: adaptive tesselation
        if isinstance(segment, Line):
            return [segment.start, segment.end]
        nSegments = math.ceil(segment.length() / self.avgTesselatedLineLength)
        return [segment.point(t / nSegments) for t in range(nSegments+1)]

    def addPath(self, object: PathObject, file: TextIO):
        objectGeo: Path = object.geometry
        for segment in objectGeo.segments:
            if isinstance(segment, Line):
                self.penMove(segment.start, file, True)
                self.penMove(segment.end, file)
            elif isinstance(segment, Arc):
                self.penMove(segment.point(0), file, True)
                if abs(abs(segment.u) - abs(segment.v)) <= .001:
                    centerOffset = segment.center - segment.point(0)
                    end = segment.point(1)
                    params = {"G": "2", "X": end.real, "Y": end.imag, "I": centerOffset.real, "J": centerOffset.imag, "E": segment.length()}
                    if segment.sweep < 0:
                        params["G"] = "3"
                    self.addLine(params, file, State.DRAW)
                else:
                    points = self.tesselate(segment)
                    for point in points:
                        self.penMove(point, file)
            elif isinstance(segment, (QuadraticBezier, CubicBezier)):
                self.penMove(segment.start, file, True)
                points = self.tesselate(segment)
                for point in points:
                    self.penMove(point, file)
            else:
                print(f"Unknown path type {type(segment)}")
            if self.showBB:
                self._moveRect(segment.bounds(), file, State._SEGMENT_BOUNDS)
        if self.showBB:
            self._moveRect(objectGeo.bounds(), file, State._PATH_BOUNDS)

    def createFile(self, geom: Document, fileOut: str, prefixFile: str = "", suffixFile: str = ""):
        try:
            with open(fileOut, "w") as destFile:
                replace = {
                    "TRAVEL_HEIGHT": self.heights[State.TRAVEL],
                    "TRAVEL_SPEED": self.speeds[State.TRAVEL],
                    "TRAVEL_ACCEL": self.accels[State.TRAVEL],
                    "LINE_WIDTH": self.width,
                    "LOAD_DELAY": self.loadDelay,
                }
                if self.showPenPos:
                    replace["BED_EXCLUDE_AREA"] = f"0x0,256x0,256x256,{self.drawableArea[0]}x256,{self.drawableArea[0]}x{256-self.drawableArea[1]},0x{256-self.drawableArea[1]}"
                    replace["EXTRUDER_OFFSET"] = f"{self.offset[0]}x{self.offset[1]}"
                else:
                    replace["BED_EXCLUDE_AREA"] = f"0x0,256x0,256x{256-self.drawableArea[1]},{256-self.drawableArea[0]}x{256-self.drawableArea[1]},{256-self.drawableArea[0]}x256,0x256"
                    replace["EXTRUDER_OFFSET"] = "0x2" # 0x2 is the default offset

                with open(prefixFile, "r") as srcFile:
                    self.fileAppend(srcFile, destFile, replace)

                for object in geom.objects:
                    self.addPath(object, destFile)
                if self.showBB:
                    self._moveRect(geom.bounds(), destFile, State._DOCUMENT_BOUNDS)

                with open(suffixFile, "r") as srcFile:
                    self.fileAppend(srcFile, destFile, replace)
            print("Post process completed sucessfully")
        except PermissionError as e:
            print(f'Could not open file "{e.filename}". Another program might be editing it.')
        except FileNotFoundError as e:
            print(f'Could not find file "{e.filename}".')

#region parseSvg

def readStyle(element: svgelements.SVGElement) -> Style:
    return Style(
        strokeWidth=getattr(element, "stroke_width", 1),
        #TODO: implement color conversion (hex -> rgb)
        #strokeColor=getattr(element, "stroke", [0, 0, 0]),
        #fillColor=getattr(element, "fill", [0, 0, 0])
    )

def parseSvgElement(node: svgelements.SVGElement, transform: Transform, document: Document):
    transform @= Transform(getattr(node, "transform", None))
    if isinstance(node, svgelements.Rect):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = transform

        # pylance seems to think node.x and node.y are None (they are actually floats)
        xmin = node.x
        xmax = node.x + node.width # type: ignore
        ymin = node.y * 1j # type: ignore
        ymax = (node.y + node.height) * 1j # type: ignore
        temp += Line(xmin+ymin, xmin+ymax)
        temp += Line(xmin+ymax, xmax+ymax)
        temp += Line(xmax+ymax, xmax+ymin)
        temp += Line(xmax+ymin, xmin+ymin)
        document.add(temp)
    elif isinstance(node, (svgelements.Circle, svgelements.Ellipse)):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = transform

        center = node.cx + node.cy*1j # type: ignore
        temp += Arc(center, node.rx, node.ry * 1j) # type: ignore
        document.add(temp)
    elif isinstance(node, svgelements.Path):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = transform

        current: complex = 0
        start = None
        for part in node:
            if isinstance(part, svgelements.Move):
                start = part.end
                current = part.end
            elif isinstance(part, svgelements.Line):
                temp += Line(current, part.end)
                current = part.end
            elif isinstance(part, svgelements.Arc):
                u = part.prx - part.center # type: ignore
                v = part.pry - part.center # type: ignore
                r = part.start - part.center

                det = u.real*v.imag - u.imag*v.real # type: ignore
                alpha = (r.real*v.imag - r.imag*v.real) / det # type: ignore
                beta = (u.real*r.imag - u.imag*r.real) / det # type: ignore

                temp += Arc(part.center, u, v, math.atan2(beta, alpha), part.sweep) # type: ignore
            elif isinstance(part, svgelements.QuadraticBezier):
                temp += QuadraticBezier(current, part.control, part.end)
                current = part.end
            elif isinstance(part, svgelements.CubicBezier):
                temp += CubicBezier(current, part.control1, part.control2, part.end)
                current = part.end
            elif isinstance(part, svgelements.Close):
                temp += Line(current, start)
                current = start
            else:
                print(f"Unknown path element: {type(part)} (part of {node.id})")
        document.add(temp)
    elif isinstance(node, svgelements.Group):
        for child in node:
            parseSvgElement(child, transform, document)
    # isinstance() won't work on SVGElement because it encapsulates all other svg classes
    elif isinstance(node, svgelements.SVG) or type(node) == svgelements.svgelements.SVGElement:
        pass # these element types can be safely ignored because they are not geometry
    else:
        print(f"Ignored {type(node)} with name {node.id}")

def parseSvg(svgPath: str, dimensions: complex, offset: complex) -> Document:
    document = Document()
    svg = svgelements.SVG.parse(svgPath)
    transform = Transform()
    #TODO: add warning when document height and width don't match
    transform.scale(svg.viewbox.height / svg.height) # undo svgelements trying to scale document to viewport
    transform.scale(dimensions.imag / svg.height) # scale to print area

    for child in svg:
        parseSvgElement(child, transform, document)
    for path in document.objects:
        # transform to printer space
        path.transform *= [1, 0, 0, -1, -offset.real, 256-offset.imag]
        path.applyTransformations()
    return document

#endregion parseSvg

plotter = Plotter()

document = parseSvg(fileIn, complex(plotter.drawableArea[0], plotter.drawableArea[1]), complex(plotter.offset[0], plotter.offset[1]))
plotter.createFile(document, fileOut, prefixFile, suffixFile)

input() # wait for user to press enter before closing window
