import cProfile
import pstats
from lib.plot import Plotter
from lib.svgparse import parseSvg
from lib.infill import generateInfill
from lib.route import orderPaths

# plot settings
fileIn = "testDrawing.svg" # hardcoded to speed up testing, need to ask user later
fileOut = "testDrawing.gcode"

plotter = Plotter("settings.json")

def run():
    document = parseSvg(fileIn, complex(plotter.settings.drawableArea[0], plotter.settings.drawableArea[1]), complex(plotter.settings.penOffset[0], plotter.settings.penOffset[1]))
    generateInfill(document, plotter.settings.infillSpacing, plotter.settings.tessellationTolerance, plotter.settings.maxTessellationDepth)
    if plotter.settings.optimizePathOrder:
        orderPaths(document, complex(plotter.pos["X"], plotter.pos["Y"]), complex(plotter.settings.endPos[0], plotter.settings.endPos[1]))
    plotter.createFile(document, fileOut)

if plotter.settings.profiling:
    profiler = cProfile.Profile()
    profiler.enable()
    run()
    profiler.disable()
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(30)
else:
    run()

input() # wait for user to press enter before closing window
