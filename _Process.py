import math
from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import TextIO
import svgelements
#TODO: use dataclasses

class States(Enum):
    DRAW = auto()
    TRAVEL = auto()

# plot settings
fileIn = "testDrawing.svg" # hardcoded to speed up testing, need to ask user later
fileOut = "testDrawing.gcode"
prefixFile = "ーstartCode.gcode"
suffixFile = "ーendCode.gcode"

penHeights = {States.DRAW: 1, States.TRAVEL: 5} # pen heights in mm
penWidth = .7 # pen width in mm (only used for display)

shortTravelThreshold = .7 # travels below this distance will not lift the pen
penPos: list[float] = [128, 128, 10] # initial pen position
drawableArea = (215.9, 230) #TODO: implement bounds checking

#region shapeDefs

# stores an object style (line width, color, fill)
class Style:
    def __init__(self, strokeWidth: float = 1, strokeColor: list[int] = [0, 0, 0], fillColor: list[int] = [0, 0, 0]):
        self.strokeWidth = strokeWidth
        self.strokeColor = strokeColor
        self.fillColor = fillColor

    def __repr__(self):
        return f"Style(strokeWidth={self.strokeWidth}, strokeColor={self.strokeColor!r}, fillColor={self.fillColor!r})"

# stores an affine transformation (rotation, scaling, shear, transform)
class Transform: #TODO: add rotate, translate, etc. functions
    def __init__(self, matrix: list[float] = [1.0, 0, 0, 1, 0, 0]):
        self.matrix = matrix

    def __repr__(self):
        return f"Transform(matrix={self.matrix!r})"

    def __matmul__(self, other: list[float]):
        return Transform(self._getTransform(other))

    def __imatmul__(self, other: list[float]):
        return Transform(self._getTransform(other))

    def _getTransform(self, other: list[float]):
        return [
            self.matrix[0]*other[0] + self.matrix[2]*other[1],
            self.matrix[1]*other[0] + self.matrix[3]*other[1],
            self.matrix[0]*other[2] + self.matrix[2]*other[3],
            self.matrix[1]*other[2] + self.matrix[3]*other[3],
            self.matrix[0]*other[4] + self.matrix[2]*other[5] + self.matrix[4],
            self.matrix[1]*other[4] + self.matrix[3]*other[5] + self.matrix[5]
        ]

    def apply(self, p: complex):
        x, y = p.real, p.imag

        return complex(
            self.matrix[0]*x + self.matrix[2]*y + self.matrix[4],
            self.matrix[1]*x + self.matrix[3]*y + self.matrix[5]
        )

    def translate(self, x: float, y: float | None = None):
        if not y:
            y = x
        self.matrix = self._getTransform([1, 0, 0, 1, x, y])

    def scale(self, sx: float, sy: float | None = None):
        if not sy:
            sy = sx
        self.matrix = self._getTransform([sx, 0, 0, sy, 0, 0])

    def rotate(self, angle: float, cx: float = 0, cy: float = 0):
        rot = [math.cos(math.radians(angle)), math.sin(math.radians(angle)), -math.sin(math.radians(angle)), math.cos(math.radians(angle)), 0, 0]
        if cx == 0 and cy == 0:
            self.matrix = self._getTransform(rot)
        else:
            self.matrix = self._getTransform([1, 0, 0, 1, cx, cy])
            self.matrix = self._getTransform(rot)
            self.matrix = self._getTransform([1, 0, 0, 1, -cx, -cy])

    def skewX(self, angle: float):
        self.matrix = self._getTransform([1, 0, math.tan(math.radians(angle)), 1, 0, 0])

    def skewY(self, angle: float):
        self.matrix = self._getTransform([1, math.tan(math.radians(angle)), 0, 1, 0, 0])

# wrapper for different types of path segments
class Segment(ABC):
    @abstractmethod
    def __repr__(self):
        pass

    @abstractmethod
    def length(self) -> float:
        """Return the arc length"""

    @abstractmethod
    def point(self, t: float) -> complex:
        """Return the point at t"""

    @abstractmethod
    def applyTransform(self, t: Transform):
        """Apply an affine transformation"""

    @abstractmethod
    def reverse(self):
        """Reverse the segment direction"""

    @abstractmethod
    def bounds(self) -> tuple[float, float, float, float]:
        """Return (xmin, ymin, xmax, ymax)"""

class Line(Segment):
    def __init__(self, start: complex = 0, end: complex = 0):
        self.start = start
        self.end = end

    def __repr__(self):
        return f"Line(start={self.start}, end={self.end})"

    def length(self) -> float:
        d = self.start - self.end
        return math.sqrt(d.real**2 + d.imag**2)

    def point(self, t: float) -> complex:
        return t * (self.end-self.start) + self.start

    def applyTransform(self, t: Transform):
        self.start = t.apply(self.start)
        self.end = t.apply(self.end)

    def reverse(self):
        self.end, self.start = self.start, self.end

    def bounds(self) -> tuple[float, float, float, float]:
        xmin = min(self.start.real, self.end.real)
        xmax = max(self.start.real, self.end.real)
        ymin = min(self.start.imag, self.end.imag)
        ymax = max(self.start.imag, self.end.imag)
        return (xmin, ymin, xmax, ymax)

class Arc(Segment):
    #TODO: storage

    def length(self) -> float: #TODO
        return 0

    def point(self, t: float) -> complex: #TODO
        return 0

    def applyTransform(self, t: Transform): #TODO
        pass

    def reverse(self): #TODO
        pass

    def bounds(self) -> tuple[float, float, float, float]: #TODO
        return (0, 0, 0, 0)

class QuadraticBezier(Segment):
    def __init__(self, start: complex = 0, p1: complex = 0, end: complex = 0):
        self.start = start
        self.p1 = p1
        self.end = end

    def length(self) -> float: #TODO
        return 0

    def point(self, t: float) -> complex: #TODO
        return 0

    def applyTransform(self, t: Transform): #TODO
        pass

    def reverse(self): #TODO
        pass

    def bounds(self) -> tuple[float, float, float, float]: #TODO
        return (0, 0, 0, 0)

class CubicBezier(Segment):
    def __init__(self, start: complex = 0, p1: complex = 0, p2: complex = 0, end: complex = 0):
        self.start = start
        self.p1 = p1
        self.p2 = p2
        self.end = end

    def length(self) -> float: #TODO
        return 0

    def point(self, t: float) -> complex: #TODO
        return 0

    def applyTransform(self, t: Transform): #TODO
        pass

    def reverse(self): #TODO
        pass

    def bounds(self) -> tuple[float, float, float, float]: #TODO
        return (0, 0, 0, 0)

# stores a list of segments
class Path:
    def __init__(self):
        self.segments: list[Segment] = []

    def __repr__(self):
        return f"Path(segments={self.segments!r})"

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
class PathObject:
    def __init__(self, id: str):
        self.id = id
        self.geometry = Path()
        self.style = Style()
        self.transform = Transform()

    def __repr__(self):
        return f"PathObject(id={self.id!r}, geometry={self.geometry}, style={self.style}, transform={self.transform})"

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

    def add(self, obj: PathObject): #FIXME: adding an object with id that already exists will break the relation between objects and id
        self.objects.append(obj)
        if obj.id is not None:
            self.id[obj.id] = obj

    def __repr__(self):
        return f"Document(id={self.id!r})"

#endregion shapeDefs

def readStyle(element: svgelements.SVGElement) -> Style:
    return Style(
        strokeWidth=getattr(element, "stroke_width", 1),
        #TODO: implement color conversion (hex -> rgb)
        #strokeColor=getattr(element, "stroke", [0, 0, 0]),
        #fillColor=getattr(element, "fill", [0, 0, 0])
    )

def parseSvg(svgPath: str):
    document = Document()
    svg = svgelements.SVG.parse(svgPath)
    scale = drawableArea[1] / svg.height #TODO: add warning when document height and width don't match
    for element in svg.elements():
        match type(element):
            case svgelements.Rect:
                builder = PathObject(element.id)
                builder.style = readStyle(element)
                builder.transform.scale(scale)
                builder.transform @= getattr(element, "transform", [1, 0, 0, 1, 0, 0])

                xmin = element.x
                xmax = element.x + element.width
                ymin = element.y * 1j
                ymax = (element.y + element.height) * 1j
                builder += Line(xmin+ymin, xmin+ymax)
                builder += Line(xmin+ymax, xmax+ymax)
                builder += Line(xmax+ymax, xmax+ymin)
                builder += Line(xmax+ymin, xmin+ymin)
                document.add(builder)
            case svgelements.SVG | svgelements.SVGElement:
                pass # these element types can be safely ignored because they are not geometry
            case _:
                print(f"Ignored {type(element)} with name {element.id}")
    for path in document.objects:
        path.applyTransformations()
    return document

# adds the contents of srcFile to the end of destFile
def fileAppend(srcFile: TextIO, destFile: TextIO):
    for line in srcFile:
        destFile.write(line)

# adds a gcode line to the file with the specified arguments
def addLine(args: dict[str, str | float], file: TextIO):
    line = ""
    lineIsValid = False # lines must contain x, y, or z arg
    for param, val in args.items():
        # check if param is not already set to current value
        val = float(val)
        match param:
            case "X":
                if val == penPos[0]:
                    continue
                penPos[0] = val
                lineIsValid = True
            case "Y":
                val = drawableArea[1] - val # flip point along y axis
                if val == penPos[1]:
                    continue
                penPos[1] = val
                lineIsValid = True
            case "Z":
                if val == penPos[2]:
                    continue
                penPos[2] = val
                lineIsValid = True

        line += f"{param}{f"{val:.5f}".rstrip("0").rstrip(".")} "
    if lineIsValid:
        file.write(line.strip() + "\n")

# moves pen to the specified location
def penMove(pos: complex, file: TextIO, travel: bool = False):
    distSquared = (pos.real - penPos[0]) ** 2 + (pos.imag - (drawableArea[1] - penPos[1])) ** 2
    if distSquared >= .000001: # moves shorter than .001 mm are probably caused by rounding errors
        if travel:
            if distSquared >= shortTravelThreshold ** 2: # long travel
                addLine({"G": "1", "Z": penHeights[States.TRAVEL]}, file)
                addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file)
                addLine({"G": "1", "Z": penHeights[States.DRAW]}, file)
            else: # short travel
                addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file)
        else: # draw moves
            if penPos[2] != penHeights[States.DRAW]:
                addLine({"G": "1", "Z": penHeights[States.DRAW]}, file)
            addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag), "E": "1"}, file)

def addPath(object: PathObject, file: TextIO):
    objectGeo: Path = object.geometry
    for segment in objectGeo.segments:
        if isinstance(segment, Line):
            penMove(segment.start, file, True)
            penMove(segment.end, file)
        elif isinstance(segment, Arc):
            print("Ignoring arc")
        elif isinstance(segment, QuadraticBezier):
            print("Ignoring quadratic bezier")
        elif isinstance(segment, CubicBezier):
            print("Ignoring cubic bezier")

document = parseSvg(fileIn)

try:
    with open(fileOut, "w") as destFile:
        with open(prefixFile, "r") as srcFile:
            fileAppend(srcFile, destFile)
        with open(fileIn, "r") as srcFile:
            destFile.write(f"; LINE_WIDTH: {penWidth}\n")
            for object in document.objects:
                addPath(object, destFile)
        with open(suffixFile, "r") as srcFile:
            fileAppend(srcFile, destFile)
    print("Post process completed sucessfully")
except PermissionError as e:
    print(f'Could not open file "{e.filename}". Another program might be editing it.')
except FileNotFoundError as e:
    print(f'Could not find file "{e.filename}".')
input() # wait for user to press enter before closing window
