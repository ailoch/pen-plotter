import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from lib.geometry import Document, Line, Path, PathObject, Segment, Transform
from lib.plot import _canvasBoundsNozzle
from lib.settings import LineType, Settings

# one row/block of a calibration sheet. height/speed/accel become PathObject.overrides
# on the emitted object - left None, that setting is simply not overridden
@dataclass
class Pass:
    label: str # the swept value's display text; also this pass's gcode object id
    geometry: list[Path] # the test pattern itself, nozzle space, lineType already STROKE
    labelOrigin: complex | None = None # baseline-left corner of the drawn label; None skips it
    height: float | None = None # mm
    speed: float | None = None # mm/min
    accel: float | None = None # mm/s^2

#region prompts

# asks for a single number in [lo, hi], re-asking on bad input. No defaults - every
# calibration value is something the user is actively trying to find
def promptNumber(prompt: str, lo: float = -math.inf, hi: float = math.inf) -> float:
    while True:
        raw = input(prompt)
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            print(f"'{raw}' is not a number.")
        elif not (lo <= value <= hi):
            if lo == -math.inf:
                print(f"Please enter a value below {hi:g}.")
            elif hi == math.inf:
                print(f"Please enter a value above {lo:g}.")
            else:
                print(f"Please enter a value between {lo:g} and {hi:g}.")
        else:
            return value

# asks for the min/max/step trio for a calibration sweep. step's sign is ignored.
# boundsCheck, if given, is called with (lo, hi, step) once those are otherwise valid;
# returning a message re-asks the whole trio with that message printed, None accepts it.
# Prints the resulting pass count before returning
def promptRamp(quantity: str, unit: str, boundsCheck: Callable[[float, float, float], str | None] | None = None) -> tuple[float, float, float]:
    while True:
        lo = promptNumber(f"Minimum {quantity} ({unit}): ")
        hi = promptNumber(f"Maximum {quantity} ({unit}): ")
        if hi <= lo:
            print(f"Maximum must be greater than the minimum ({lo:g}).")
            continue
        step = abs(promptNumber(f"Step ({unit}): "))
        if step == 0:
            print("Step must not be zero.")
            continue
        problem = boundsCheck(lo, hi, step) if boundsCheck else None
        if problem is not None:
            print(problem)
            continue
        print(f"This will draw {len(_rampValues(lo, hi, step))} passes.")
        return lo, hi, step

#endregion

#region layout

# the swept values from lo to hi in steps of step - inclusive of hi when step divides
# the range evenly, never overshooting past hi when it doesn't
def _rampValues(lo: float, hi: float, step: float) -> list[float]:
    step = abs(step)
    count = math.floor((hi - lo) / step + 1e-9) + 1
    return [lo + i * step for i in range(count)]

# shifts every segment of every path in place by offset
def _translatePaths(paths: list[Path], offset: complex):
    if offset == 0:
        return
    t = Transform()
    t.translate(offset.real, offset.imag)
    for path in paths:
        for segment in path.segments:
            segment.applyTransform(t)

# moves a locally-built pattern (drawn around its own origin) so it sits in the middle
# of bounds (as returned by lib.plot._canvasBoundsNozzle) - every pass's geometry and
# labelOrigin move together. A pattern with no geometry at all is returned unchanged
def _centerPasses(passes: list[Pass], bounds: tuple[float, float, float, float]) -> list[Pass]:
    xmin, ymin, xmax, ymax = math.inf, math.inf, -math.inf, -math.inf
    for p in passes:
        for path in p.geometry:
            pxmin, pymin, pxmax, pymax = path.bounds()
            xmin, ymin = min(xmin, pxmin), min(ymin, pymin)
            xmax, ymax = max(xmax, pxmax), max(ymax, pymax)
    if xmin > xmax:
        return passes

    patternCenter = complex((xmin + xmax) / 2, (ymin + ymax) / 2)
    canvasCenter = complex((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    offset = canvasCenter - patternCenter

    for p in passes:
        _translatePaths(p.geometry, offset)
        if p.labelOrigin is not None:
            p.labelOrigin += offset
    return passes

#endregion

#region stroke font

# a minimal digit font in a normalized box (0,0) to (_GLYPH_WIDTH,1). Each glyph is a
# tuple of strokes; each stroke is an ordered run of points drawn as one connected
# polyline (the pen only lifts between strokes) - unlike a bag of independent (start,
# end) lines, a Path.tessellate() run assumes its segments are one continuous curve
# and can silently splice two that merely happen to be collinear
_GLYPH_WIDTH = 0.6
_GLYPH_ADVANCE = 0.8 # glyph width plus inter-glyph gap, in em units (multiples of capHeight)

_TL, _TR = complex(0, 1), complex(_GLYPH_WIDTH, 1)
_ML, _MR = complex(0, 0.5), complex(_GLYPH_WIDTH, 0.5)
_BL, _BR = complex(0, 0), complex(_GLYPH_WIDTH, 0)

_GLYPHS: dict[str, tuple[tuple[complex, ...], ...]] = {
    "0": ((_TL, _TR, _BR, _BL, _TL),),
    "1": ((_TR, _BR),),
    "2": ((_TL, _TR, _MR, _ML, _BL, _BR),),
    "3": ((_TL, _TR, _MR, _BR, _BL), (_ML, _MR)),
    "4": ((_TL, _ML, _MR, _TR), (_MR, _BR)),
    "5": ((_BL, _BR, _MR, _ML, _TL, _TR),),
    "6": ((_ML, _MR, _BR, _BL, _ML, _TL, _TR),),
    "7": ((_TL, _TR, _BR),),
    "8": ((_TL, _TR, _BR, _BL, _TL), (_ML, _MR)),
    "9": ((_BL, _BR, _MR, _TR, _TL, _ML, _MR),),
    ".": ((complex(_GLYPH_WIDTH * .3, 0), complex(_GLYPH_WIDTH * .5, 1 / 6)),),
}

# width of text if drawn at the given cap height, including its trailing glyph gap
def _textWidth(text: str, capHeight: float) -> float:
    return len(text) * _GLYPH_ADVANCE * capHeight

# builds text as STROKE geometry, one Path per stroke, with the first glyph's
# baseline-left corner at origin. An unrecognized character (there's no glyph for
# "-") still advances the cursor
def _textPaths(text: str, origin: complex, capHeight: float) -> list[Path]:
    paths: list[Path] = []
    cursor = 0.0
    for ch in text:
        for stroke in _GLYPHS.get(ch, ()):
            points = [origin + complex(cursor, 0) + p * capHeight for p in stroke]
            segments: list[Segment] = [Line(points[i], points[i + 1]) for i in range(len(points) - 1)]
            paths.append(Path(segments, LineType.STROKE))
        cursor += _GLYPH_ADVANCE * capHeight
    return paths

#endregion

_LABEL_CAP_HEIGHT = 5.0 # mm - legible at a glance and in a photo without crowding the pattern next to it

#region ruler sweep

# shared by height/speed (and any later test whose rows are one plain horizontal
# line): rows are stacked bottom-to-top at a tight, pen-relative pitch, ticked longer
# every 5th row and longer still every 10th, ruler-style, with only the 10th carrying a value label.
_RULER_PITCH_FACTOR = 3.5 # * penWidth, as tight a pitch as the pen can resolve
_RULER_LABEL_GAP = 3.0 # mm - between a row's right edge and its label
_RULER_TICK_5 = 1.5 # mm - extra length every 5th row gets
_RULER_TICK_10 = 3.0 # mm - extra length every 10th row gets, instead of the 5th-row tick

# the three row shapes a ruler sweep picks between: (plain, every 5th, every 10th).
# Each is drawn in its own local space with the row's baseline on y=0.
_RulerShapes = tuple[list[Path], list[Path], list[Path]]

# the plain-horizontal-line shapes, ticked by the standard amounts - what a sweep whose
# rows are just a single line of the given length wants
def _lineShapes(length: float) -> _RulerShapes:
    def line(extra: float) -> list[Path]:
        return [Path([Line(0j, complex(length + extra, 0))], LineType.STROKE)]
    return line(0), line(_RULER_TICK_5), line(_RULER_TICK_10)

# builds one Pass per swept value via makePass(value, geometry, labelOrigin), tiling the
# matching shape up the stack. Shapes are sized in fixed mm rather than as a fraction of
# the canvas, so the stack sits well inside the canvas regardless of whether the canvas
# bounds themselves are accurate yet (that's the edge test's job)
def _rulerSweep(quantity: str, unit: str, shapes: _RulerShapes, settings: Settings,
                 makePass: Callable[[float, list[Path], complex | None], Pass], extraRowHeight: float = 0) -> list[Pass]:
    lo, hi, step = promptRamp(quantity, unit)
    pitch = _RULER_PITCH_FACTOR*settings.penWidth + extraRowHeight
    passes = []
    for i, v in enumerate(_rampValues(lo, hi, step)):
        labelled = i % 10 == 0
        y = i * pitch
        geometry = deepcopy(shapes[2] if labelled else shapes[1] if i % 5 == 0 else shapes[0])
        _translatePaths(geometry, complex(0, y))
        if i % 2:
            # reversing leaves the row looking identical but drawn end-to-start, so
            # the pen finishes where the next row begins - a pitch-sized hop instead
            # of a full-width trip back
            geometry.reverse()
            for path in geometry:
                path.reverse()
        # placed off the row's own right edge, and its baseline dropped half a cap
        # height so the label's height centers on the row instead of sitting above it
        labelOrigin = complex(max(p.bounds()[2] for p in geometry) + _RULER_LABEL_GAP, y - _LABEL_CAP_HEIGHT / 2) if labelled else None
        passes.append(makePass(v, geometry, labelOrigin))
    return _centerPasses(passes, _canvasBoundsNozzle(settings))

#endregion

#region height test

def _heightTest(settings: Settings) -> list[Pass]:
    return _rulerSweep("height", "mm", _lineShapes(10), settings,
                        lambda z, geometry, labelOrigin: Pass(f"{z:g}", geometry, labelOrigin, height=z))

#endregion

#region speed test

def _speedTest(settings: Settings) -> list[Pass]:
    return _rulerSweep("speed", "mm/s", _lineShapes(50), settings,
                        lambda v, geometry, labelOrigin: Pass(f"{v:g}", geometry, labelOrigin, speed=v * 60))

#endregion

#region accel test

# a straight line can't show an acceleration problem - nothing on it ever changes
# direction. Each row is a single continuous zigzag instead: a wide swing (general
# overshoot) running straight into a narrow one (tight corners), so both scales of
# cornering error show up in one stroke rather than costing a pen lift between them
_ACCEL_WIDE_PITCH = 5.0 # mm - period of the wide zigzag
_ACCEL_WIDE_AMPLITUDE = 4.0 # mm - peak-to-peak
_ACCEL_WIDE_TEETH = 4
_ACCEL_NARROW_PITCH = 2.0 # mm - period of the narrow zigzag
_ACCEL_NARROW_AMPLITUDE = 1.5 # mm - peak-to-peak
_ACCEL_NARROW_TEETH = 6
_ACCEL_TICK_5_TEETH = 2 # extra narrow-pitch teeth every 5th row gets
_ACCEL_TICK_10_TEETH = 4 # extra narrow-pitch teeth every 10th row gets, instead of the 5th-row tick

# nTeeth points continuing a zigzag from start, alternating up/down starting with up
# (or down, if startUp is False). Returns the new points (start itself excluded, so
# callers can concatenate legs point-to-point) and whether the *next* leg should
# start by going up - lets a leg of one amplitude hand off cleanly to another
def _zigzagLeg(start: complex, startUp: bool, nTeeth: int, pitch: float, amplitude: float) -> tuple[list[complex], bool]:
    points = []
    x = start.real
    up = startUp
    for _ in range(nTeeth):
        x += pitch / 2
        points.append(complex(x, amplitude / 2 if up else -amplitude / 2))
        up = not up
    return points, up

# the wide zigzag running straight into the narrow one, starting at the bottom (so
# the first stroke is a clean rise, not a lift-off from mid-air) and ending at the
# bottom or top depending on whether the tooth counts are even or odd. Ticked by
# appending 1-2 more narrow-pitch teeth rather than a plain dash, so a tick still
# tests acceleration instead of being a bare ruler mark
def _zigzagShapes() -> _RulerShapes:
    start = complex(0, -_ACCEL_WIDE_AMPLITUDE / 2)
    wide, up = _zigzagLeg(start, True, _ACCEL_WIDE_TEETH, _ACCEL_WIDE_PITCH, _ACCEL_WIDE_AMPLITUDE)
    narrow, up = _zigzagLeg(wide[-1], up, _ACCEL_NARROW_TEETH, _ACCEL_NARROW_PITCH, _ACCEL_NARROW_AMPLITUDE)
    base = [start, *wide, *narrow]
    def row(tickTeeth: int) -> list[Path]:
        points = base
        if tickTeeth:
            tick, _ = _zigzagLeg(base[-1], up, tickTeeth, _ACCEL_NARROW_PITCH, _ACCEL_NARROW_AMPLITUDE)
            points = base + tick
        segments: list[Segment] = [Line(points[i], points[i + 1]) for i in range(len(points) - 1)]
        return [Path(segments, LineType.STROKE)]
    return row(0), row(_ACCEL_TICK_5_TEETH), row(_ACCEL_TICK_10_TEETH)

def _accelTest(settings: Settings) -> list[Pass]:
    return _rulerSweep("acceleration", "mm/s^2", _zigzagShapes(), settings,
                        lambda a, geometry, labelOrigin: Pass(f"{a:g}", geometry, labelOrigin, accel=a), 2)

#endregion

# registered calibration tests, keyed by the name settings.calibrationTest must match.
# each entry prompts for its own parameters and returns the sheet's passes; populated
# by the individual tests as they're added
CALIBRATION_TESTS: dict[str, Callable[[Settings], list[Pass]]] = {"height": _heightTest, "speed": _speedTest, "accel": _accelTest}

# whether settings.calibrationTest selects a calibration sheet instead of an SVG
# drawing, warning if it names no registered test. This is where that value gets
# validated, since lib.settings can't import the registry without a cycle
def calibrationEnabled(settings: Settings) -> bool:
    name = settings.calibrationTest
    if name == "none":
        return False
    if name not in CALIBRATION_TESTS:
        options = ", ".join(f"'{n}'" for n in CALIBRATION_TESTS)
        print(f"Warning: processing.calibrationTest in the machine config is set to '{name}', which is not a "
              f"recognized calibration test. Set it to 'none' to draw normally, or one of: {options}.")
        return False
    return True

# builds the sheet settings.calibrationTest selects, prompting for that test's own
# parameters. Its geometry is already final, so unlike a parsed SVG it goes straight
# to gcode without the stroke/infill stages
def generateCalibration(settings: Settings) -> Document:
    document = Document()
    for p in CALIBRATION_TESTS[settings.calibrationTest](settings):
        overrides = {k: v for k, v in (("height", p.height), ("speed", p.speed), ("accel", p.accel)) if v is not None}
        document.add(PathObject(p.label, p.geometry, overrides=overrides))

        if p.labelOrigin is None:
            continue
        labelPaths = _textPaths(p.label, p.labelOrigin, _LABEL_CAP_HEIGHT)
        if labelPaths:
            # a label is drawn exactly like the row it names so it degrades with that row
            document.add(PathObject(p.label + " label", labelPaths, overrides=dict(overrides)))
    return document
