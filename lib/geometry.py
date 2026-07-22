import math
from abc import ABC, abstractmethod
from typing import Self
from dataclasses import dataclass, field
from scipy.integrate import quad
from lib.settings import LineType

# stores an object style (line width, color, fill)
@dataclass
class Style:
    strokeWidth: float = 1
    # none means no stroke (the SVG default)
    strokeColor: list[int] | None = None
    linejoin: str = "miter" # "miter" | "round" | "bevel"
    linecap: str = "butt" # "butt" | "round" | "square"
    miterlimit: float = 4 # SVG default
    dasharray: list[float] | None = None #TODO: dash generation
    # none means no fill
    fillColor: list[int] | None = field(default_factory=lambda: [0, 0, 0])
    fillRule: str = "nonzero" # SVG default; the other valid value is "evenodd"

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

    # samples this segment into points no more than tolerance (mm) from the true
    # curve, including both endpoints. Cheap recursive midpoint subdivision -
    # safe here because a single Segment is smooth (no interior corners), so the
    # max chord deviation is always near the middle. subclasses with an exact
    # formula (Line, Arc) override this
    def toPoints(self, tolerance: float) -> list[complex]:
        points: list[complex] = []
        def recurse(t0: float, t1: float, p0: complex, p1: complex):
            pm = self.point((t0 + t1) / 2)
            chord = p1 - p0
            if abs(chord) < 1e-9:
                dev = abs(pm - p0)
            else:
                dev = abs(((pm - p0) * chord.conjugate() / abs(chord)).imag)
            if dev <= tolerance:
                points.append(p0)
            else:
                mid = (t0 + t1) / 2
                recurse(t0, mid, p0, pm)
                recurse(mid, t1, pm, p1)
        recurse(0.0, 1.0, self.point(0.0), self.point(1.0))
        points.append(self.point(1.0))
        return points

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

    def toPoints(self, tolerance: float) -> list[complex]:
        return [self.start, self.end]

@dataclass
class Arc(Segment):
    center: complex = 0
    u: complex = 0
    v: complex = 0
    t0: float = 0
    sweep: float = 2*math.pi # sweep is between -2pi (ccw) and 2pi (cw)

    # returns an Arc that passes through the given points, or None if they're
    # too close to collinear to define one. maxRadiusToChord rejects circles
    # whose radius exceeds that multiple of the p0-p1 chord length: for near-
    # collinear input, the circumcircle math below is numerically unstable
    # (tiny input noise can swing the computed radius wildly), and a genuinely
    # well-fit circle's chord is always a meaningful fraction of its radius
    @classmethod
    def fromThreePoints(cls, p0: complex, pm: complex, p1: complex, maxRadiusToChord: float | None = None) -> "Arc | None":
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
        if maxRadiusToChord is not None and radius > maxRadiusToChord * max(abs(p1 - p0), 1e-9):
            return None

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

    # samples this Arc into points fine enough that no point deviates from the
    # true circle by more than tolerance (mm) - unlike tessellate(), this works
    # for elliptical (non-circular) arcs too, since it's purely angle-step-based
    def toPoints(self, tolerance: float) -> list[complex]:
        radius = abs(self.u)
        sweep = abs(self.sweep)
        if radius < 1e-9 or sweep < 1e-9:
            return [self.point(0.0), self.point(1.0)]

        # max angular step such that the chord's sagitta stays within tolerance
        cosVal = max(-1.0, min(1.0, 1 - tolerance / radius))
        thetaMax = max(math.acos(cosVal), 1e-3)
        numSteps = max(1, math.ceil(sweep / (2 * thetaMax)))
        return [self.point(i / numSteps) for i in range(numSteps + 1)]

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
    lineType: LineType = LineType.PERIMETER

    def length(self) -> float:
        len = 0
        for segment in self.segments:
            len += segment.length()
        return len

    def start(self) -> complex:
        return self.segments[0].point(0)

    def end(self) -> complex:
        return self.segments[-1].point(1)

    # returns the point at a single normalized parameter spanning the WHOLE
    # subpath (0 <= t <= 1, t=1 is the path's end), resolving which segment t
    # falls in - lets tessellate() sample continuously across segment boundaries
    # instead of being confined to one segment's own [0,1] range
    def point(self, t: float) -> complex:
        n = len(self.segments)
        s = min(max(t, 0.0), 1.0) * n
        i = min(int(s), n - 1)
        return self.segments[i].point(s - i)

    def isClosed(self, tolerance: float = 1e-6) -> bool:
        return abs(self.start() - self.end()) < tolerance

    # returns whether this path encloses non-zero area - distinct from isClosed():
    # an open path can still enclose area and a closed path can still enclose zero
    # area (a degenerate out-and-back trace)
    def isFillable(self) -> bool:
        N_SAMPLES = 8
        pts: list[complex] = []
        for segment in self.segments:
            for i in range(N_SAMPLES):
                pts.append(segment.point(i / N_SAMPLES))
        if len(pts) < 3:
            return False

        # shoelace formula, implicitly closing back to the first point
        area = 0.0
        for i in range(len(pts)):
            p0, p1 = pts[i], pts[(i+1) % len(pts)]
            area += p0.real*p1.imag - p1.real*p0.imag
        return abs(area) / 2 > 1e-6

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

    # deviation-check samples per original segment touched by a candidate fit
    # range (see _tryFitRange)
    _SAMPLES_PER_SEGMENT = 10

    # circumcircle radius/chord cutoff passed to Arc.fromThreePoints (see there)
    _MAX_RADIUS_TO_CHORD = 20.0

    # tries to fit [t0,t1] (in Path.point()'s 0..1 space) to a single Line or
    # circular Arc within tolerance (mm); returns None if neither fits. Line is
    # tried first so a near-straight range is never represented as an arc.
    def _tryFitRange(self, t0: float, t1: float, tolerance: float, allowArcs: bool) -> "Segment | None":
        n = len(self.segments)
        p0 = self.point(t0)
        p1 = self.point(t1)

        # sample each original segment touched by [t0,t1] within its own
        # sub-range, not spread evenly across the whole range - otherwise a
        # large range dominated by one straight segment can starve a small
        # curved fragment of samples. Also test every segment boundary
        # explicitly, since it's the only place a corner can hide between
        # otherwise-smooth per-segment samples.
        firstSeg = min(int(t0 * n), n - 1)
        lastSeg = min(int(t1 * n), n - 1) if t1 < 1.0 else n - 1
        sampleTs: list[float] = []
        allLines = True
        for segIdx in range(firstSeg, lastSeg + 1):
            segT0 = max(t0, segIdx / n)
            segT1 = min(t1, (segIdx + 1) / n)
            if segT1 <= segT0:
                continue
            if segT0 > t0:
                sampleTs.append(segT0) # exact boundary - see corner note above
            if isinstance(self.segments[segIdx], Line):
                sampleTs.append((segT0 + segT1) / 2)
            else:
                allLines = False
                for i in range(1, self._SAMPLES_PER_SEGMENT + 1):
                    sampleTs.append(segT0 + (segT1 - segT0) * (i / (self._SAMPLES_PER_SEGMENT + 1)))

        samplePts = [self.point(t) for t in sampleTs]

        # --- try a Line ---
        # early-exits on the first out-of-tolerance sample instead of scanning
        # every point to find the true max - most candidates fail the Line
        # check quickly when there's real curvature, so this avoids paying for
        # the full sample set on the common failing case
        chord = p1 - p0
        chordLen = abs(chord)
        fits = True
        if chordLen < 1e-9:
            for p in samplePts:
                if abs(p - p0) > tolerance:
                    fits = False
                    break
        else:
            chordConj = (chord / chordLen).conjugate()
            for p in samplePts:
                rel = (p - p0) * chordConj
                along = max(0.0, min(chordLen, rel.real))
                if math.hypot(rel.real - along, rel.imag) > tolerance:
                    fits = False
                    break

        if fits:
            return Line(p0, p1)

        # --- try a circular Arc via 3-point circumcircle ---
        if allowArcs:
            if t0 == 0.0 and t1 == 1.0 and self.isClosed():
                # average every vertex because 3 evenly spread out points is too noisy
                direction = Arc.fromThreePoints(p0, self.point(1/3), self.point(2/3), maxRadiusToChord=self._MAX_RADIUS_TO_CHORD)
                arc = None
                if direction is not None:
                    vertices = [self.point(i / n) for i in range(n)]
                    center = sum(vertices) / n
                    radius = sum(abs(v - center) for v in vertices) / n
                    arcT0 = math.atan2(-(p0 - center).imag, (p0 - center).real)
                    arc = Arc(center=center, u=complex(radius, 0), v=complex(0, -radius), t0=arcT0, sweep=math.copysign(math.tau, direction.sweep))
            else:
                pm = self.point((t0 + t1) / 2)
                arc = Arc.fromThreePoints(p0, pm, p1, maxRadiusToChord=self._MAX_RADIUS_TO_CHORD)
            if arc is not None:
                center, r = arc.center, abs(arc.u)
                fits = True
                if allLines:
                    # a range of pure Lines has no interior curvature of its own
                    # (each sample is already an exact data point, plus each
                    # segment's own midpoint - see above), so there's no "out
                    # and back" risk within any one sample gap: plain radial
                    # deviation is enough, and skips the angle math below
                    for p in samplePts:
                        if abs(abs(p - center) - r) > tolerance:
                            fits = False
                            break
                else:
                    # distance to the finite swept arc, not radial deviation
                    # from the underlying circle - catches a point that's
                    # radially close but at a totally different, unswept angle
                    for p in samplePts:
                        rel = p - center
                        theta = math.atan2(-rel.imag, rel.real)
                        u = arc._thetaToT(theta)
                        dev = min(abs(p - arc.point(0.0)), abs(p - arc.point(1.0))) if u is None else abs(abs(rel) - r)
                        if dev > tolerance:
                            fits = False
                            break
                if fits:
                    return arc

        return None

    # from tStart, finds the farthest extent toward tLimit that still fits a
    # single Line/Arc, via exponential growth then a binary search between the
    # last success and first failure. backward=True grows leftward from tStart
    # instead of rightward. The extent is approximate, not the exact farthest
    # fitting t - a slightly-short extent only shifts more work onto the
    # neighboring fit, it never risks exceeding tolerance.
    def _greedyExtent(self, tStart: float, tLimit: float, tolerance: float, allowArcs: bool, backward: bool = False) -> tuple[float, "Segment"]:
        span = (tStart - tLimit) if backward else (tLimit - tStart)
        n = len(self.segments)

        def fitDelta(delta: float) -> "Segment | None":
            if backward:
                return self._tryFitRange(tStart - delta, tStart, tolerance, allowArcs)
            return self._tryFitRange(tStart, tStart + delta, tolerance, allowArcs)

        # a minimal (half-segment) range should always fit
        lo = min(span, 0.5 / n)
        found = fitDelta(lo)
        if found is None:
            # pathological fallback: fall back to a direct Line
            lo = min(span, 1e-6)
            a, b = (tStart - lo, tStart) if backward else (tStart, tStart + lo)
            found = Line(self.point(a), self.point(b))

        if lo >= span:
            end = tStart - lo if backward else tStart + lo
            return end, found

        # exponential growth from lo
        hi = span
        size = lo
        while size < span:
            nextSize = min(size * 2, span)
            fit = fitDelta(nextSize)
            if fit is None:
                hi = nextSize
                break
            lo, found, size = nextSize, fit, nextSize
        else:
            end = tStart - span if backward else tStart + span
            return end, found

        # binary search between lo (known good) and hi (known bad); stop once
        # within ~2% of a segment's length rather than fully converging
        minStep = (span / n) * 0.02
        while hi - lo > minStep:
            mid = (lo + hi) / 2
            fit = fitDelta(mid)
            if fit is not None:
                lo, found = mid, fit
            else:
                hi = mid

        end = tStart - lo if backward else tStart + lo
        return end, found

    # fits [t0,t1] to a sequence of Lines/circular Arcs within tolerance (mm):
    # greedily consumes the most range a single Line/Arc can cover from each
    # side, then recurses on whatever's left in the middle, rather than
    # blindly splitting at the midpoint (which can break an otherwise-straight
    # run just because a corner falls near it - "/\______" should become "/",
    # "\", "______", not "/", "\", "__", "____").
    def _fitRange(self, t0: float, t1: float, tolerance: float, allowArcs: bool) -> list["Segment"]:
        tf, front = self._greedyExtent(t0, t1, tolerance, allowArcs)
        if tf >= t1:
            return [front] # front's search already reached t1

        tb, back = self._greedyExtent(t1, t0, tolerance, allowArcs, backward=True)

        if tf >= tb:
            # front and back overlap in [tf,tb] - re-fit [tf,t1] directly rather
            # than algebraically trimming back's [tb,t1] fit: a trimmed Arc's
            # start would come from interpolating back's own parametrization
            # (constant angular speed), which only matches the true curve at
            # back's original 3 defining points - anywhere else, including
            # exactly at tf, it's off by up to tolerance, leaving a visible
            # gap/overlap against front's endpoint. A fresh fit's p0 is always
            # exactly self.point(tf), so it always joins front with no gap.
            trimmed = self._tryFitRange(tf, t1, tolerance, allowArcs)
            if trimmed is not None:
                return [front, trimmed]
            return [front] + self._fitRange(tf, t1, tolerance, allowArcs)

        return [front] + self._fitRange(tf, tb, tolerance, allowArcs) + [back]

    # returns a new Path made of only Lines/circular Arcs, fit to within
    # tolerance (mm) of the original curves. non-mutating. if allowArcs is
    # False, the result is Lines only.
    #
    # with allowArcs, segments already in final form (Lines, circular Arcs) are
    # passed through untouched; only curves that still need fitting (Beziers,
    # non-circular Arcs) go through the bidirectional fitter, which fits across
    # their shared boundaries (see _fitRange) - so already-tessellated input
    # (e.g. re-tessellating for gcode output) is nearly free. Set fitLines=True
    # to instead treat every Line as raw data to be re-fit too (e.g. a dense
    # polyline of individually-meaningless points, like a pyclipper offset
    # result) - see Path.fromPoints.
    def tessellate(self, tolerance: float, allowArcs: bool = True, fitLines: bool = False) -> "Path":
        n = len(self.segments)
        if n == 0:
            return Path([], lineType=self.lineType)

        # Lines-only output is only ever consumed as a flattened polygon (infill),
        # which needs points within tolerance, not a minimal segment count - so
        # skip the fitter entirely and cheaply subdivide each segment to points
        if not allowArcs:
            flat: list[Segment] = []
            for segment in self.segments:
                pts = segment.toPoints(tolerance)
                flat.extend(Line(pts[i], pts[i+1]) for i in range(len(pts) - 1))
            return Path(flat, lineType=self.lineType)

        def isSimple(segment: Segment) -> bool:
            if isinstance(segment, Line):
                return not fitLines
            return isinstance(segment, Arc) and abs(abs(segment.u) - abs(segment.v)) <= tolerance

        newSegments: list[Segment] = []
        runStart: int | None = None

        # merges an exactly-collinear, same-direction consecutive Line into the
        # previous one instead of appending it - joins redundant straight splits
        # (a passed-through Line meeting the Line a neighboring fit ended on).
        # Exact collinearity only, so it never approximates a curve as one line
        def appendMerging(segment: Segment):
            if isinstance(segment, Line) and newSegments and isinstance(newSegments[-1], Line):
                prev = newSegments[-1]
                d0, d1 = prev.end - prev.start, segment.end - segment.start
                cross = d0.real*d1.imag - d0.imag*d1.real
                if abs(cross) <= 1e-9 * abs(d0) * abs(d1) and (d0.real*d1.real + d0.imag*d1.imag) >= 0:
                    newSegments[-1] = Line(prev.start, segment.end)
                    return
            newSegments.append(segment)

        for i, segment in enumerate(self.segments):
            if isSimple(segment):
                if runStart is not None:
                    for s in self._fitRange(runStart / n, i / n, tolerance, allowArcs):
                        appendMerging(s)
                    runStart = None
                appendMerging(segment)
            elif runStart is None:
                runStart = i
        if runStart is not None:
            for s in self._fitRange(runStart / n, 1.0, tolerance, allowArcs):
                appendMerging(s)

        return Path(newSegments, lineType=self.lineType)

    # builds a Path connecting the given points with Lines; if
    # closed, an additional Line connects the last point back to the first
    @classmethod
    def fromPoints(cls, points: list[complex], closed: bool = False) -> "Path":
        segments = [Line(points[i], points[i+1]) for i in range(len(points)-1)]
        if closed:
            segments.append(Line(points[-1], points[0]))
        return cls(segments) # type: ignore

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

        # stroke-width (and dash lengths) are in user units, so they need to scale
        # with the transform same as the geometry does. sqrt(|det|) of the 2x2 part
        # is the uniform-scale equivalent of a possibly non-uniform transform - a
        # single width/length can't represent true non-uniform stroke scaling anyway
        m = self.transform.matrix
        scale = abs(m[0]*m[3] - m[1]*m[2]) ** 0.5
        self.style.strokeWidth *= scale
        if self.style.dasharray is not None:
            self.style.dasharray = [d * scale for d in self.style.dasharray]

        self.transform = Transform() # reset transformation

    def start(self) -> complex:
        return self.geometry[0].start()

    def end(self) -> complex:
        return self.geometry[-1].end()

    # returns the point at normalized parameter t (0 <= t <= 1) along the given
    # subpath - see Path.point()
    def point(self, subpathIndex: int, t: float) -> complex:
        return self.geometry[subpathIndex].point(t)

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
