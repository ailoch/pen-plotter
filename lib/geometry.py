import math
from abc import ABC, abstractmethod
from typing import Self
from dataclasses import dataclass, field
from scipy.integrate import quad

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

    # returns other@self instead of self@other
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

        # bambu studio renderer breaks if very large coordinates are given
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
        # line derivative is constant, so t is irrelevant
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

# stores a list of paths, style, and transform
@dataclass
class PathObject:
    id: str
    geometry: list[Path] = field(default_factory=lambda: [Path()])
    style: Style = field(default_factory=Style)
    transform : Transform = field(default_factory=Transform)

    def __iadd__(self, segment):
        self.geometry[-1].segments.append(segment)
        return self

    def applyTransformations(self):
        for path in self.geometry:
            for segment in path.segments:
                segment.applyTransform(self.transform)
        self.transform = Transform() # reset transformation

    def start(self) -> complex:
        return self.geometry[0].start()

    def end(self) -> complex:
        return self.geometry[-1].end()

    # only true for a single closed loop
    def isClosed(self) -> bool:
        return len(self.geometry) == 1 and self.geometry[0].isClosed()

    def vertices(self) -> list[complex]:
        if not self.isClosed():
            raise ValueError("PathObject.vertices() is not valid on multi-subpath or open objects")
        return self.geometry[0].vertices()

    def rotateTo(self, index: int):
        if not self.isClosed():
            raise ValueError("PathObject.rotateTo() is not valid on multi-subpath or open objects")
        self.geometry[0].rotateTo(index)

    # reverses the whole object: subpath order and each subpath's own direction
    def reverse(self):
        self.geometry.reverse()
        for path in self.geometry:
            path.reverse()

    def bounds(self) -> tuple[float, float, float, float]:
        bounds = (math.inf, math.inf, -math.inf, -math.inf)
        for path in self.geometry:
            pathBounds = path.bounds()
            bounds = (min(bounds[0], pathBounds[0]), min(bounds[1], pathBounds[1]), max(bounds[2], pathBounds[2]), max(bounds[3], pathBounds[3]))
        return bounds

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
            segmentBounds = object.bounds()
            bounds = (min(bounds[0], segmentBounds[0]), min(bounds[1], segmentBounds[1]), max(bounds[2], segmentBounds[2]), max(bounds[3], segmentBounds[3]))
        return bounds
