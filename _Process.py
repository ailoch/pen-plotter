import math
from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import TextIO, Self
from dataclasses import dataclass, field, fields
from scipy.integrate import quad
import svgelements, commentjson

class State(Enum):
    DRAW = auto()
    TRAVEL = auto()
    _SEGMENT_BOUNDS = auto()
    _PATH_BOUNDS = auto()
    _DOCUMENT_BOUNDS = auto()

# plot settings
fileIn = "testDrawing.svg" # hardcoded to speed up testing, need to ask user later
fileOut = "testDrawing.gcode"

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
    # recursively fits self[t0:t1] to a single Line or circular Arc within tolerance
    # (mm), falling back to bisecting the range when neither fits. works for any
    # segment type since it only samples self.point(t) (valid for any real t)
    def _fitToTolerance(self, t0: float, t1: float, tolerance: float, maxDepth: int, depth: int = 0) -> list["Segment"]:
        N_SAMPLES = 5

        p0 = self.point(t0)
        p1 = self.point(t1)
        sampleTs = [t0 + (t1-t0) * (i / (N_SAMPLES+1)) for i in range(1, N_SAMPLES+1)]
        samplePts = [self.point(t) for t in sampleTs]

        if depth >= maxDepth:
            print(f"Warning: tessellation of {type(self).__name__} exceeded maxTessellationDepth ({maxDepth}); falling back to a straight line, tolerance may be exceeded")
            return [Line(p0, p1)]

        # --- try a Line ---
        chord = p1 - p0
        chordLen = abs(chord)
        if chordLen < 1e-9:
            # zero-length chord: fall back to distance from p0 directly
            maxDev = max((abs(p - p0) for p in samplePts), default=0.0)
        else:
            chordDir = chord / chordLen
            # rotate (p - p0) into the chord's frame; the imaginary part is then the
            # perpendicular distance from the chord
            maxDev = max((abs(((p - p0) * chordDir.conjugate()).imag) for p in samplePts), default=0.0)

        if maxDev <= tolerance:
            return [Line(p0, p1)]

        # --- try a circular Arc via 3-point circumcircle ---
        tm = (t0 + t1) / 2
        pm = self.point(tm)

        arc = Arc.fromThreePoints(p0, pm, p1)
        if arc is not None:
            maxRadialDev = max((abs(abs(p - arc.center) - abs(arc.u)) for p in samplePts), default=0.0)
            if maxRadialDev <= tolerance:
                return [arc]

        # --- neither fit: split and recurse ---
        left = self._fitToTolerance(t0, tm, tolerance, maxDepth, depth+1)
        right = self._fitToTolerance(tm, t1, tolerance, maxDepth, depth+1)
        return left + right

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

    @abstractmethod
    def tessellate(self, tolerance: float, maxDepth: int) -> list["Segment"]:
        """Return a list of line/arc segments approximating this segment within a tolerance (mm)"""

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

    def tessellate(self, tolerance: float, maxDepth: int) -> list[Segment]:
        return [self]

@dataclass
class Arc(Segment):
    center: complex = 0
    u: complex = 0
    v: complex = 0
    t0: float = 0
    sweep: float = 2*math.pi # sweep is between -2pi (ccw) and 2pi (cw)

    # returns an Arc that passes through the given points
    @classmethod
    def fromThreePoints(cls, p0: complex, pm: complex, p1: complex) -> "Arc | None":
        ax, ay = p0.real, p0.imag
        bx, by = pm.real, pm.imag
        cx, cy = p1.real, p1.imag

        d = 2 * (ax*(by-cy) + bx*(cy-ay) + cx*(ay-by))

        # d = |p1-p0| * |pm-p0| * sin(theta), theta being the angle at p0 between the
        # two chords - dividing it out gives sin(theta) directly
        sideProduct = abs(pm-p0) * abs(p1-p0)
        if sideProduct < 1e-12 or abs(d / sideProduct) < 1e-9:
            return None

        ax2ay2 = ax*ax + ay*ay
        bx2by2 = bx*bx + by*by
        cx2cy2 = cx*cx + cy*cy

        centerX = (ax2ay2*(by-cy) + bx2by2*(cy-ay) + cx2cy2*(ay-by)) / d
        centerY = (ax2ay2*(cx-bx) + bx2by2*(ax-cx) + cx2cy2*(bx-ax)) / d

        center = complex(centerX, centerY)
        radius = abs(p0 - center)

        # angles of the 3 points around the center (negated imag to match the Arc's
        # u=(r,0)/v=(0,-r) basis, where increasing angle sweeps clockwise / +sweep = G2)
        a0 = math.atan2(-(p0-center).imag, (p0-center).real)
        am = math.atan2(-(pm-center).imag, (pm-center).real)
        a1 = math.atan2(-(p1-center).imag, (p1-center).real)

        dm = (am - a0 + math.pi) % math.tau - math.pi # am relative to a0, unwrapped to (-pi, pi]

        # pick the winding that actually sweeps through pm, not the long way around
        candidates = [(a1 - a0) + k*math.tau for k in (-2, -1, 0, 1, 2)]
        sweep = min(candidates, key=lambda cand: abs(cand/2 - dm))

        return cls(center=center, u=complex(radius, 0), v=complex(0, -radius), t0=a0, sweep=sweep)

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

    def tessellate(self, tolerance: float, maxDepth: int) -> list[Segment]:
        if abs(abs(self.u) - abs(self.v)) <= tolerance:
            return [self] # circular arcs don't need to be tesselated
        return self._fitToTolerance(0.0, 1.0, tolerance, maxDepth)

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

    def tessellate(self, tolerance: float, maxDepth: int) -> list[Segment]:
        return self._fitToTolerance(0.0, 1.0, tolerance, maxDepth)

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

    def tessellate(self, tolerance: float, maxDepth: int) -> list[Segment]:
        return self._fitToTolerance(0.0, 1.0, tolerance, maxDepth)

# stores a list of segments
@dataclass
class Path:
    segments: list[Segment] = field(default_factory=list)

    def length(self) -> float:
        len = 0
        for segment in self.segments:
            len += segment.length()
        return len

    def start(self) -> complex:
        return self.segments[0].point(0)

    def end(self) -> complex:
        return self.segments[-1].point(1)

    def isClosed(self, tolerance: float = 1e-6) -> bool:
        return abs(self.start() - self.end()) < tolerance

    # returns the points where segments of the path meet
    def vertices(self) -> list[complex]:
        verts = [segment.point(0) for segment in self.segments]
        if not self.isClosed(): # closed paths have the same start and end
            verts.append(self.end())
        return verts

    # re-splits a closed path so segments[index] becomes the first segment drawn
    def rotateTo(self, index: int):
        if not self.isClosed():
            raise ValueError("Path.rotateTo() is not valid on open paths")
        self.segments = self.segments[index:] + self.segments[:index]

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

    # returns a new Path made of only Lines/circular Arcs, fit to within tolerance
    # (mm) of the original curves. non-mutating - leaves self untouched
    def tessellate(self, tolerance: float, maxDepth: int) -> "Path":
        newSegments: list[Segment] = []
        for segment in self.segments:
            newSegments.extend(segment.tessellate(tolerance, maxDepth))
        return Path(newSegments)

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

@dataclass
class PlotSettings:
    # machine settings
    startPos: dict[str, float] = field(default_factory=lambda: {"X": 0, "Y": 0, "Z": 10})
    penOffset: tuple[float, float] = (0, 0)
    plateSize: tuple[float, float] = (150, 150)
    drawableArea: tuple[float, float] = (150, 150)

    # gcode settings
    heights: dict[State, float] = field(default_factory=dict)
    speeds: dict[State, float] = field(default_factory=dict)
    accels: dict[State, float] = field(default_factory=dict)
    shortTravelThreshold: float = .5
    tessellationTolerance: float = .05
    maxTessellationDepth: int = 20

    prefixFile: str = ""
    suffixFile: str = ""

    # visualization settings
    penWidth: float = .5
    lineTypes: dict[State, str] = field(default_factory=dict)
    loadDelay: float = 20
    showPenPos: bool = True
    style: str = "line type"
    styleLineOrder: list[str] = field(default_factory=list)
    optimizePathOrder: bool = True

    # debug settings
    showBoundingBoxes: bool = False

    def initFromJson(self, path):
        with open(path) as f:
            data = commentjson.load(f)
        allowed = {f.name for f in fields(PlotSettings)}

        for sectionName, data in data.items():
            for settingName, setting in data.items():
                #TODO: check types of incoming objects
                if settingName in allowed:
                    match settingName: # some properties need special logic
                        case "heights" | "speeds" | "accels" | "lineTypes":
                            temp = {}
                            for k, v in setting.items():
                                match k:
                                    case "draw":
                                        temp[State.DRAW] = v
                                    case "travel":
                                        temp[State.TRAVEL] = v
                                    case "_segmentBounds":
                                        temp[State._SEGMENT_BOUNDS] = v
                                    case "_pathBounds":
                                        temp[State._PATH_BOUNDS] = v
                                    case "_documentBounds":
                                        temp[State._DOCUMENT_BOUNDS] = v
                                    case _:
                                        print(f"Unknown move type '{k}' (reading {sectionName}.{settingName})")
                            setattr(self, settingName, temp)
                        case "penOffset" | "plateSize" | "drawableArea":
                            setattr(self, settingName, tuple(setting))
                        case "startPos":
                            self.startPos = dict(zip(("X", "Y", "Z"), setting))
                        case "style":
                            allowedStyles = ("line type", "instruction", "segment")
                            if setting.lower() in allowedStyles:
                                self.style = setting.lower()
                            else:
                                print(f"Unknown style '{setting}' (reading {sectionName}.style)")
                        case _:
                            setattr(self, settingName, setting)
                else:
                    print(f"Unknown setting {sectionName}.{settingName}")

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
    def penMove(self, pos: complex, file: TextIO, travel: bool = False, lineType: State | None = None):
        distSquared = (pos.real - self.pos["X"]) ** 2 + (pos.imag - self.pos["Y"]) ** 2
        if distSquared >= .000001: # moves shorter than .001 mm are probably caused by rounding errors
            if travel:
                if distSquared >= self.settings.shortTravelThreshold ** 2: # long travel
                    self.addLine({"G": "1", "Z": self.settings.heights[State.TRAVEL]}, file, State.TRAVEL)
                    self.addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file)
                    self.addLine({"G": "1", "Z": self.settings.heights[State.DRAW]}, file)
                else: # short travel
                    self.addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag)}, file, State.DRAW)
            else: # draw moves
                if self.pos["Z"] != self.settings.heights[lineType or State.DRAW]:
                    self.addLine({"G": "1", "Z": self.settings.heights[lineType or State.DRAW]}, file)
                self.addLine({"G": "1", "X": str(pos.real), "Y": str(pos.imag), "E": math.hypot(pos.real-self.pos["X"], pos.imag-self.pos["Y"])}, file, lineType or State.DRAW)

    def addPath(self, object: PathObject, file: TextIO):
        tessellated = object.geometry.tessellate(self.settings.tessellationTolerance, self.settings.maxTessellationDepth)
        for segment in tessellated.segments:
            if isinstance(segment, Line):
                self.penMove(segment.start, file, True)
                self.penMove(segment.end, file)
            elif isinstance(segment, Arc):
                self.penMove(segment.point(0), file, True)
                centerOffset = segment.center - segment.point(0)
                end = segment.point(1)
                params = {"G": "2", "X": end.real, "Y": end.imag, "I": centerOffset.real, "J": centerOffset.imag, "E": segment.length()}
                if segment.sweep < 0:
                    params["G"] = "3"
                self.addLine(params, file, State.DRAW)
            else:
                print(f"Unknown path type {type(segment)}")
        if self.settings.showBoundingBoxes:
            for segment in object.geometry.segments:
                self._moveRect(segment.bounds(), file, State._SEGMENT_BOUNDS)
            self._moveRect(object.geometry.bounds(), file, State._PATH_BOUNDS)

    def createFile(self, geom: Document, fileOut: str):
        try:
            with open(fileOut, "w") as destFile:
                replace: dict[str, float | str] = {
                    "TRAVEL_HEIGHT": self.settings.heights[State.TRAVEL],
                    "TRAVEL_SPEED": self.settings.speeds[State.TRAVEL],
                    "TRAVEL_ACCEL": self.settings.accels[State.TRAVEL],
                    "LINE_WIDTH": self.settings.penWidth,
                    "LOAD_DELAY": self.settings.loadDelay,
                }
                if self.settings.showPenPos:
                    replace["BED_EXCLUDE_AREA"] = f"0x0,256x0,256x256,{self.settings.drawableArea[0]}x256,{self.settings.drawableArea[0]}x{256-self.settings.drawableArea[1]},0x{256-self.settings.drawableArea[1]}"
                    replace["EXTRUDER_OFFSET"] = f"{self.settings.penOffset[0]}x{self.settings.penOffset[1]}"
                else:
                    replace["BED_EXCLUDE_AREA"] = f"0x0,256x0,256x{256-self.settings.drawableArea[1]},{256-self.settings.drawableArea[0]}x{256-self.settings.drawableArea[1]},{256-self.settings.drawableArea[0]}x256,0x256"
                    replace["EXTRUDER_OFFSET"] = "0x2" # 0x2 is the default offset

                with open(self.settings.prefixFile, "r") as srcFile:
                    self.fileAppend(srcFile, destFile, replace)

                for object in geom.objects:
                    self.addPath(object, destFile)
                if self.settings.showBoundingBoxes:
                    self._moveRect(geom.bounds(), destFile, State._DOCUMENT_BOUNDS)

                with open(self.settings.suffixFile, "r") as srcFile:
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

def parseSvgElement(node: svgelements.SVGElement, docTransform: Transform, document: Document):
    nodeTransform = docTransform @ Transform(getattr(node, "transform", None))
    if isinstance(node, svgelements.Rect):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = nodeTransform

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
        temp.transform = nodeTransform

        center = node.cx + node.cy*1j # type: ignore
        temp += Arc(center, node.rx, node.ry * 1j) # type: ignore
        document.add(temp)
    elif isinstance(node, svgelements.Path):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = nodeTransform

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
            parseSvgElement(child, docTransform, document)
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
    # FIXME: scaling logic can be weird sometimes
    transform.scale(svg.viewbox.height / svg.height) # undo svgelements trying to scale document to viewport

    # this line can cause unexpected behavior sometimes
    # mabye ask user if they want to scale drawing?
    #transform.scale(dimensions.imag / svg.viewbox.height) # scale to print area

    for child in svg:
        parseSvgElement(child, transform, document)
    for path in document.objects:
        # transform to printer space
        path.transform *= [1, 0, 0, -1, -offset.real, 256-offset.imag]
        path.applyTransformations()
    return document

#endregion parseSvg

#region routing

# reorders the paths in document.objects to minimize travel distance
# open paths will be reversed as needed
# closed paths will be entered/exited at any of their vertices
# a stock TSP solver is not applicable here becuase paths can be altered
# this converges in under a second with a few hundred objects
def orderPaths(document: Document, startPos: complex = 0):
    objects = document.objects
    n = len(objects)
    if n <= 1:
        return

    starts: list[complex] = []
    ends: list[complex] = []
    closed: list[bool] = []
    vertices: list[list[complex]] = []
    for obj in objects:
        starts.append(obj.geometry.start())
        ends.append(obj.geometry.end())
        isClosed = obj.geometry.isClosed()
        closed.append(isClosed)
        vertices.append(obj.geometry.vertices() if isClosed else [])

    rev = [False] * n # whether open path i is currently drawn end->start
    anchor = [0] * n # for closed path i, index into vertices[i] of the chosen entry/exit point

    def startPt(i: int) -> complex:
        if closed[i]:
            return vertices[i][anchor[i]]
        return ends[i] if rev[i] else starts[i]

    def endPt(i: int) -> complex:
        if closed[i]:
            return vertices[i][anchor[i]]
        return starts[i] if rev[i] else ends[i]

    # nearest-neighbor construction
    remaining = set(range(n))
    order: list[int] = []
    current = startPos
    while remaining:
        def closestDist(i: int) -> float:
            if closed[i]:
                return min(abs(current-v) for v in vertices[i])
            return min(abs(current-starts[i]), abs(current-ends[i]))
        best = min(remaining, key=closestDist)
        if closed[best]:
            anchor[best] = min(range(len(vertices[best])), key=lambda k: abs(current-vertices[best][k]))
        else:
            rev[best] = abs(current-ends[best]) < abs(current-starts[best])
        order.append(best)
        current = endPt(best)
        remaining.discard(best)

    # 2-opt + anchor-optimization refinement, interleaved until neither helps anymore
    improved = True
    while improved:
        improved = False

        # 2-opt: try reversing every possible run of the tour
        for i in range(n):
            leftPrev = startPos if i == 0 else endPt(order[i-1])
            for j in range(i, n):
                oldLeft = abs(leftPrev - startPt(order[i]))
                oldRight = 0.0 if j == n-1 else abs(endPt(order[j]) - startPt(order[j+1]))

                newLeft = abs(leftPrev - endPt(order[j])) # order[j] reversed becomes the new start
                newRight = 0.0 if j == n-1 else abs(startPt(order[i]) - startPt(order[j+1])) # order[i] reversed becomes the new end

                if newLeft + newRight < oldLeft + oldRight - 1e-9:
                    order[i:j+1] = reversed(order[i:j+1])
                    for k in range(i, j+1):
                        rev[order[k]] = not rev[order[k]]
                    improved = True

        # anchor optimization: for each closed path, try every vertex as the entry/exit point
        for i in range(n):
            objIdx = order[i]
            if not closed[objIdx]:
                continue
            leftNeighbor = startPos if i == 0 else endPt(order[i-1])
            rightNeighbor = None if i == n-1 else startPt(order[i+1])

            def anchorCost(v: complex) -> float:
                return abs(leftNeighbor-v) + (0.0 if rightNeighbor is None else abs(v-rightNeighbor))

            bestK = min(range(len(vertices[objIdx])), key=lambda k: anchorCost(vertices[objIdx][k]))
            if bestK != anchor[objIdx] and anchorCost(vertices[objIdx][bestK]) < anchorCost(vertices[objIdx][anchor[objIdx]]) - 1e-9:
                anchor[objIdx] = bestK
                improved = True

    for i in range(n):
        if closed[i]:
            objects[i].geometry.rotateTo(anchor[i])
        elif rev[i]:
            objects[i].geometry.reverse()
    document.objects = [objects[i] for i in order]

#endregion routing

plotter = Plotter("settings.json")

document = parseSvg(fileIn, complex(plotter.settings.drawableArea[0], plotter.settings.drawableArea[1]), complex(plotter.settings.penOffset[0], plotter.settings.penOffset[1]))
if plotter.settings.optimizePathOrder:
    orderPaths(document, complex(plotter.pos["X"], plotter.pos["Y"]))
plotter.createFile(document, fileOut)

input() # wait for user to press enter before closing window
