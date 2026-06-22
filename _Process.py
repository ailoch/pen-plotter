import math
from abc import ABC, abstractmethod
import svgelements
#TODO: use dataclasses

# plot settings
fileIn = "testDrawing.svg" # hardcoded to speed up testing
# will ask user later

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
    def __init__(self, matrix: list[float] = [1, 0, 0, 1, 0, 0]):
        self.matrix = matrix

    def __repr__(self):
        return f"Transform(matrix={self.matrix!r})"

    def __imatmul__(self, other: list[float]):
        return Transform([
            self.matrix[0]*other[0] + self.matrix[2]*other[1],
            self.matrix[1]*other[0] + self.matrix[3]*other[1],
            self.matrix[0]*other[2] + self.matrix[2]*other[3],
            self.matrix[1]*other[2] + self.matrix[3]*other[3],
            self.matrix[0]*other[4] + self.matrix[2]*other[5] + self.matrix[4],
            self.matrix[1]*other[4] + self.matrix[3]*other[5] + self.matrix[5]
        ])

    def apply(self, p: complex):
        x, y = p.real, p.imag

        return complex(
            self.matrix[0]*x + self.matrix[2]*y + self.matrix[4],
            self.matrix[1]*x + self.matrix[3]*y + self.matrix[5]
        )

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

class Arc:
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

class QuadraticBezier:
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

class CubicBezier:
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
    for element in svg.elements():
        match type(element):
            case svgelements.Rect:
                builder = PathObject(element.id)
                builder.style = readStyle(element)
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

document = parseSvg(fileIn)

print(document)
