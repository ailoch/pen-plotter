import os
import cProfile
import pstats
from lib.plot import Plotter
from lib.svgparse import parseSvg, SvgParseError
from lib.infill import generateInfill
from lib.route import orderPaths

def promptInputFile() -> str:
    while True:
        path = input("Enter input file: ")
        if os.path.isfile(path) and path.lower().endswith(".svg"):
            return path
        print(f"'{path}' is not an existing SVG file.")

def promptOutputFile() -> str:
    while True:
        path = input("Enter output file: ")
        if os.path.exists(path):
            answer = input(f"'{path}' already exists. Overwrite? (y/n): ")
            if "y" in answer.lower():
                return path
            continue
        return path

fileIn = promptInputFile()
fileOut = promptOutputFile()

plotter = Plotter("settings.json")

def run() -> bool:
    try:
        document = parseSvg(fileIn, plotter.settings.drawableArea, plotter.settings.penOffset)
    except SvgParseError as e:
        print(e)
        return False

    generateInfill(document, plotter.settings.infillSpacing, plotter.settings.tessellationTolerance, plotter.settings.maxTessellationDepth)

    if plotter.settings.optimizePathOrder:
        orderPaths(document, complex(plotter.pos["X"], plotter.pos["Y"]), plotter.settings.endPos)

    plotter.createFile(document, fileOut)
    return True

def runProfiled() -> bool:
    profiler = cProfile.Profile()
    profiler.enable()
    success = run()
    profiler.disable()
    if success: # only print stats on successful run
        pstats.Stats(profiler).sort_stats("cumulative").print_stats(30)
    return success

runPipeline = runProfiled if plotter.settings.profiling else run

while not runPipeline():
    fileIn = promptInputFile()

input() # wait for user to press enter before closing window
