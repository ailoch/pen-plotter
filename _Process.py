from lib.plot import Plotter
from lib.svgparse import parseSvg
from lib.route import orderPaths

# plot settings
fileIn = "testDrawing.svg" # hardcoded to speed up testing, need to ask user later
fileOut = "testDrawing.gcode"

plotter = Plotter("settings.json")

document = parseSvg(fileIn, complex(plotter.settings.drawableArea[0], plotter.settings.drawableArea[1]), complex(plotter.settings.penOffset[0], plotter.settings.penOffset[1]))
if plotter.settings.optimizePathOrder:
    orderPaths(document, complex(plotter.pos["X"], plotter.pos["Y"]), complex(plotter.settings.endPos[0], plotter.settings.endPos[1]))
plotter.createFile(document, fileOut)

input() # wait for user to press enter before closing window
