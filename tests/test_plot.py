"""Unit tests for lib/plot.py's gcode-emitting helpers."""
import pytest

from lib.plot import _LOAD_PROGRESS_MAX_INTERVAL, _LOAD_PROGRESS_MAX_STEP, _LOAD_PROGRESS_MIN_STEP, _eValue, _waitForPen
from lib.settings import Settings

# --- E axis scaling (_eValue) -----------------------------------------------------

@pytest.mark.parametrize("multiplier,expected", [(1, 10), (0.5, 5), (2, 20)])
def testEValueScalesByTheMultiplier(multiplier, expected):
    assert _eValue(Settings(eAxisMultiplier=multiplier), 10) == expected

@pytest.mark.parametrize("multiplier", [0, -0.5, -3])
def testNonPositiveMultiplierDropsE(multiplier):
    """settings._validate warns that a multiplier <= 0 drops E from every draw move,
    so every path to that must actually produce a droppable 0. A negative one is the
    case that bites: _addLine elides falsy args, and a negative E is truthy, so it
    would emit a *retraction* on every draw move rather than nothing at all."""
    assert _eValue(Settings(eAxisMultiplier=multiplier), 10) == 0

# --- pen-load wait (_waitForPen) --------------------------------------------------

def _block(delay: float, showProgress: bool = True) -> str:
    return _waitForPen(Settings(loadDelay=delay, showLoadProgress=showProgress))

def _dwells(block: str) -> list[float]:
    return [float(l[4:]) for l in block.splitlines() if l.startswith("G4 P")]

def _percents(block: str) -> list[int]:
    return [int(l[5:]) for l in block.splitlines() if l.startswith("M73 P")]

# 0.4s/5s: 5% is the coarsest step allowed, so both step a clean 5% (at 20ms/250ms).
# 20s: 500ms is the shorter cap, so it steps by time and the percentages land at 2.5%.
# 20.3s: doesn't divide into whole milliseconds - the case that catches per-step
#        rounding drift, which every other delay here hides by dividing evenly.
# 400s: 500ms would be an eighth of a percent, so the 1% floor takes over instead.
_DELAYS = [0.4, 5, 20, 20.3, 400]

@pytest.mark.parametrize("delay", _DELAYS)
def testLoadProgressCountsDownFromFullToEmpty(delay):
    """The countdown shows time *remaining*, so it starts at 100 and lands on 0
    without ever repeating or going back up."""
    percents = _percents(_block(delay))
    assert percents[0] == 100
    assert percents[-1] == 0
    assert all(a > b for a, b in zip(percents, percents[1:])), "must decrease monotonically"

@pytest.mark.parametrize("delay", _DELAYS)
def testLoadProgressWaitsExactlyTheConfiguredDelay(delay):
    """Splitting the dwell must not change how long it lasts. Each step is rounded to
    whole milliseconds, so this only holds because the boundaries are taken from the
    cumulative time rather than by summing a rounded per-step interval."""
    assert sum(_dwells(_block(delay))) == pytest.approx(delay * 1000)

@pytest.mark.parametrize("delay", _DELAYS)
def testLoadProgressStepsWithinBothBounds(delay):
    """The step is the 500ms tick clamped into 1%..5%: fine enough to look like a
    countdown, coarse enough not to re-emit a number the screen already shows."""
    steps = len(_dwells(_block(delay)))
    assert _LOAD_PROGRESS_MIN_STEP <= 100 / steps <= _LOAD_PROGRESS_MAX_STEP

@pytest.mark.parametrize("delay", [0.4, 5, 20, 20.3])
def testLoadProgressTicksAtLeastEveryHalfSecond(delay):
    """Under the 1% floor's reach, every tick is also inside the time cap. (400s is
    excluded: there the floor deliberately wins and ticks run 4s apart.)"""
    assert max(_dwells(_block(delay))) <= _LOAD_PROGRESS_MAX_INTERVAL * 1000 + 1 # +1ms for whole-ms rounding

def testVeryLongDelayFallsBackToTheOnePercentFloor():
    """At 400s a 500ms tick is an eighth of a percent. The floor takes over, giving
    exactly the 101 whole percentages - not 800 ticks mostly repeating each other."""
    block = _block(400)
    assert _percents(block) == list(range(100, -1, -1))
    assert _dwells(block) == [4000] * 100

def testLoadProgressOffWaitsSilently():
    """Disabled, it's the plain dwell it always was - just G4 P (ms) rather than G4 S."""
    assert _block(5, showProgress=False) == "G4 P5000"

@pytest.mark.parametrize("delay", [0, -3])
def testNonPositiveDelayEmitsNoCountdown(delay):
    """Nothing to count down through - and a negative delay must not become a
    negative dwell."""
    assert _block(delay) == "G4 P0"
