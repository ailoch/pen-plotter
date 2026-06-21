from abc import ABC, abstractmethod

# wrapper for different types of path segments
class Segment(ABC):
    @abstractmethod
    def length(self) -> float:
        """Return the arc length"""

    @abstractmethod
    def point(self, t: float) -> complex:
        """Return the point at t"""

    @abstractmethod
    def transform(self, matrix: list[int]):
        """Apply an affine transformation"""

    @abstractmethod
    def reverse(self):
        """Reverse the segment direction"""
    
    @abstractmethod
    def bounds(self) -> tuple[int, int, int, int]:
        """Return (xmin, ymin, xmax, ymax)"""
    
    @abstractmethod
    def __repr__(self):
        return super().__repr__()

class Line(Segment):
    def __init__(self, start: complex = 0, end: complex = 0):
        self.start = start
        self.end = end
    
    def length(self) -> float: #TODO
        return 0
    
    def point(self, t: float) -> complex: #TODO
        return 0
    
    def transform(self, matrix: list[int]): #TODO
        pass
    
    def reverse(self): #TODO
        pass

    def bounds(self) -> tuple[int, int, int, int]: #TODO
        return (0, 0, 0, 0)
    
    def __repr__(self):
        return f"Line(start={self.start}, end={self.end})"

class Arc:
    #TODO: storage
    
    def length(self) -> float: #TODO
        return 0
    
    def point(self, t: float) -> complex: #TODO
        return 0
    
    def transform(self, matrix: list[int]): #TODO
        pass
    
    def reverse(self): #TODO
        pass

    def bounds(self) -> tuple[int, int, int, int]: #TODO
        return (0, 0, 0, 0)

class QuadraticBezier:
    start: complex = 0
    p1: complex = 0
    end: complex = 0
    
    def length(self) -> float: #TODO
        return 0
    
    def point(self, t: float) -> complex: #TODO
        return 0
    
    def transform(self, matrix: list[int]): #TODO
        pass
    
    def reverse(self): #TODO
        pass

    def bounds(self) -> tuple[int, int, int, int]: #TODO
        return (0, 0, 0, 0)

class CubicBezier:
    start: complex = 0
    p1: complex = 0
    p2: complex = 0
    end: complex = 0
    
    def length(self) -> float: #TODO
        return 0
    
    def point(self, t: float) -> complex: #TODO
        return 0
    
    def transform(self, matrix: list[int]): #TODO
        pass
    
    def reverse(self): #TODO
        pass

    def bounds(self) -> tuple[int, int, int, int]: #TODO
        return (0, 0, 0, 0)

# stores a list of segments
class Path:
    segments: list[Segment] = []

    def transform(self, matrix: list[int]): #TODO
        pass

    def length(self) -> float: #TODO
        pass

    def reverse(self): #TODO
        pass

    def bounds(self) -> tuple[int, int, int, int]: #TODO
        pass

    def tessellate(self): #TODO
        pass

    def __repr__(self):
        return f"Path(segments={self.segments!r})"

# stores an object style (line width, color, fill, etc.)
class Style:
    strokeWidth: float = 1
    strokeColor: int = [0, 0, 0]
    fillColor: float = [0, 0, 0]

    def __repr__(self):
        return f"Style(strokeWidth={self.strokeWidth}, strokeColor={self.strokeColor!r}, fillColor={self.fillColor!r})"

# stores an affine transformation (rotation, scaling, shear, transform)
class Transform: #TODO
    matrix = [1, 0, 0, 1, 0, 0] #TODO: find valid starting condition
    
    def __repr__(self):
        return f"Transform(matrix={self.matrix!r})"

# stores a path, style, and transform
class PathObject:
    geometry = Path()
    style = Style()
    transform = Transform()

    def __init__(self, id: str):
        self.id = id
    
    def __repr__(self):
        return f"PathObject(id={self.id!r}, geometry={self.geometry}, style={self.style}, transform={self.transform})"

    def __iadd__(self, segment):
        self.geometry.segments.append(segment)
        return self

# overall document
class Document:
    objects: list[PathObject] = []
    id: dict[str: PathObject] = {}

    def add(self, obj: Segment):
        self.objects.append(obj)
        if obj.id is not None:
            self.id[obj.id] = obj

    def __repr__(self):
        return f"Document(id={self.id!r})"

document = Document()

document.add(PathObject("Test"))
document.id["Test"] += Line(3+5j, 100+100j)

print(document)
