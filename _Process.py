import os, time, cProfile, pstats
from enum import Enum, auto
from lib.settings import Settings
from lib.svgparse import parseSvg, SvgParseError
from lib.infill import generateInfill
from lib.route import orderPaths
from lib.plot import createFile

settings = Settings()
settings.initFromJson("config/bambu_p1s_config.json")

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
        # empty input retries the previous file, bypassing the overwrite prompt
        # below since the user already answered it when they first entered this path
        if not path and previous is not None:
            return previous
        if os.path.exists(path):
            answer = input(f"'{path}' already exists. Overwrite? (y/n): ")
            if "y" in answer.lower():
                return path
            continue
        return path

fileIn = promptInputFile()
fileOut = promptOutputFile()

class RunResult(Enum):
    SUCCESS = auto()
    BAD_INPUT = auto() # re-prompt for the input file
    BAD_OUTPUT = auto() # re-prompt for the output file

def run() -> RunResult:
    startTime = time.perf_counter()
    try:
        document = parseSvg(fileIn, settings)
    except SvgParseError as e:
        print(e)
        return RunResult.BAD_INPUT

    generateInfill(document, settings)
    orderPaths(document, settings)

    if not createFile(document, settings, fileOut):
        return RunResult.BAD_OUTPUT

    print(f"\nGcode created successfully in {time.perf_counter() - startTime:.3f}s")
    print("Press enter to close")
    return RunResult.SUCCESS

def runProfiled() -> RunResult:
    profiler = cProfile.Profile()
    profiler.enable()
    result = run()
    profiler.disable()
    if result == RunResult.SUCCESS: # only print stats on successful run
        pstats.Stats(profiler).sort_stats("cumulative").print_stats(30)
    return result

runPipeline = runProfiled if settings.profiling else run

result = runPipeline()
while result != RunResult.SUCCESS:
    if result == RunResult.BAD_INPUT:
        fileIn = promptInputFile(fileIn)
    else:
        fileOut = promptOutputFile(fileOut)
    result = runPipeline()

input() # wait for user to press enter before closing window
