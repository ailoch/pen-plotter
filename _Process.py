import os, time, cProfile, pstats
from enum import Enum, auto
from lib.settings import Settings
from lib.svgparse import loadSvg, promptRescale, parseSvg, SvgParseError
from lib.geometry import Document
from lib.stroke import generateStroke, dropRawGeometry
from lib.infill import generateInfill
from lib.route import orderPaths
from lib.plot import createFile
from lib.calibration import calibrationEnabled, generateCalibration

settings = Settings()
settings.initFromSlicerJson("config/slicers/bambu_studio.json")
settings.initFromMachineJson("config/machines/bambu_p1s.json")

calibrating = calibrationEnabled(settings)

def promptInputFile(previous: str | None = None) -> str:
    while True:
        path = input("Enter input file: ")
        if not path and previous is not None: # empty input retries the previous file
            return previous
        if os.path.isfile(path) and path.lower().endswith(".svg"):
            return path
        print(f"'{path}' is not an existing SVG file.")

def promptOutputFile(previous: str | None = None) -> str:
    while True:
        path = input("Enter output file: ")
        if not path:
            # empty input retries the previous file, bypassing the overwrite prompt
            # below since the user already answered it when they first entered this
            # path. with no previous file there's nothing to fall back to, so an
            # empty path is rejected here rather than passed down as a bad filename
            if previous is not None:
                return previous
            print("Please enter an output file.")
            continue
        if path.lower().endswith(".svg"):
            print("Output file must not be an SVG file.")
            continue
        if os.path.exists(path):
            answer = input(f"'{path}' already exists. Overwrite? (y/n): ")
            if "y" in answer.lower():
                return path
            continue
        return path

fileIn = "" if calibrating else promptInputFile()
fileOut = f"test_{settings.calibrationTest}.gcode" if calibrating else promptOutputFile()

class RunResult(Enum):
    SUCCESS = auto()
    BAD_INPUT = auto() # re-prompt for the input file
    BAD_OUTPUT = auto() # re-prompt for the output file

# the parsed/infilled/routed drawing, cached across output-file retries: a write
# failure (e.g. the target gcode is open in another program) then only redoes the
# write, instead of re-parsing and re-asking the rescale question. Reset to None
# when the input file changes so it gets re-parsed
document: Document | None = None

# starts the run timer and, if enabled, the profiler - called once the interactive
# questions are out of the way, so user thinking time isn't counted or profiled
def startTiming() -> tuple[float, cProfile.Profile | None]:
    profiler = cProfile.Profile() if settings.profiling else None
    if profiler:
        profiler.enable()
    return time.perf_counter(), profiler

def run() -> RunResult:
    global document

    if document is None and calibrating:
        # the test's own parameter questions come first, same as promptRescale below
        sheet = generateCalibration(settings)
        startTime, profiler = startTiming()
        # already final drawable geometry: no stroke/infill to generate, and no
        # routing either, since a sweep is only readable in the order it was built
        document = sheet
    elif document is None:
        try:
            svg = loadSvg(fileIn)
        except SvgParseError as e:
            print(e)
            return RunResult.BAD_INPUT
        scaleX, scaleY = promptRescale(svg, settings)

        startTime, profiler = startTiming()

        document = parseSvg(svg, settings, scaleX, scaleY)
        generateStroke(document, settings)
        generateInfill(document, settings)
        dropRawGeometry(document)
        orderPaths(document, settings)
    else:
        # reusing an already-built document (output retry): time/profile the write only
        startTime, profiler = startTiming()

    ok = createFile(document, settings, fileOut)

    if profiler:
        profiler.disable()
    if not ok:
        return RunResult.BAD_OUTPUT

    print(f"\nGcode created successfully in {time.perf_counter() - startTime:.3f}s")
    if profiler: # only print stats on successful run
        pstats.Stats(profiler).sort_stats("cumulative").print_stats(30)
    print("Press enter to close")
    return RunResult.SUCCESS

result = run()
while result != RunResult.SUCCESS:
    if result == RunResult.BAD_INPUT:
        fileIn = promptInputFile(fileIn)
        document = None # force a re-parse of the new input file
    else:
        fileOut = promptOutputFile(fileOut)
    result = run()

input() # wait for user to press enter before closing window
