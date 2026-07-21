import math
from typing import cast
import svgelements

from lib.geometry import Style, Transform, Segment, Line, Arc, QuadraticBezier, CubicBezier, Path, PathObject, Document
from lib.settings import Settings

class SvgParseError(Exception):
    pass

def readStyle(element: svgelements.SVGElement) -> Style:
    # svgelements resolves inherited/cascaded presentation attributes (fill, fill-rule)
    # into this raw values dict, so this works the same whether the attribute is set
    # directly on the element or inherited from a parent (e.g. horse.svg's root <svg
    # fill="#000000">)
    values = getattr(element, "values", {})
    return Style(
        strokeWidth=getattr(element, "stroke_width", 1),
        fillColor=None if values.get("fill") == "none" else [0, 0, 0],
        fillRule=values.get("fill-rule", "nonzero"),
        #TODO: implement color conversion (hex -> rgb)
        #strokeColor=getattr(element, "stroke", [0, 0, 0]),
        #fillColor=getattr(element, "fill", [0, 0, 0])
    )

def parseSvgElement(node: svgelements.SVGElement, docTransform: Transform, document: Document, textNames: list[str]):
    nodeTransform = docTransform @ Transform(getattr(node, "transform", None))
    if isinstance(node, svgelements.Rect):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = nodeTransform

        x = cast(float, node.x) # cast() tells linters the correct type without affecting runtime
        y = cast(float, node.y)
        width = cast(float, node.width)
        height = cast(float, node.height)

        xmin = x
        xmax = x + width
        ymin = y * 1j
        ymax = (y + height) * 1j
        temp += Line(xmin+ymin, xmin+ymax)
        temp += Line(xmin+ymax, xmax+ymax)
        temp += Line(xmax+ymax, xmax+ymin)
        temp += Line(xmax+ymin, xmin+ymin)
        document.add(temp)
    elif isinstance(node, (svgelements.Circle, svgelements.Ellipse)):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = nodeTransform

        cx = cast(float, node.cx)
        cy = cast(float, node.cy)
        rx = cast(float, node.rx)
        ry = cast(float, node.ry)

        center = cx + cy*1j
        temp += Arc(center, rx, ry * 1j)
        document.add(temp)
    elif isinstance(node, svgelements.Path):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = nodeTransform
        temp.geometry = [] # built explicitly below so each Move starts a new subpath

        current: complex = 0
        start = None
        currentSegments: list[Segment] = []

        # finalize supath when segments are collected
        def finalizeSubpath():
            if currentSegments:
                temp.geometry.append(Path(currentSegments.copy()))

        for part in node:
            if isinstance(part, svgelements.Move):
                finalizeSubpath()
                currentSegments = []
                start = part.end
                current = part.end
            elif isinstance(part, svgelements.Line):
                currentSegments.append(Line(current, part.end))
                current = part.end
            elif isinstance(part, svgelements.Arc):
                # svgelements represents this in its own Point class rather than a
                # builtin complex - Point implements the same .real/.imag/arithmetic
                # protocol complex does (by design, per its own docstring, as a
                # drop-in replacement), so this codebase treats the two
                # interchangeably; cast() documents that instead of suppressing the
                # whole line, and (unlike # type: ignore) still lets every other
                # expression on these lines be checked normally
                arcCenter = cast(complex, part.center)
                arcStart = cast(complex, part.start)
                prx = cast(complex, part.prx)
                pry = cast(complex, part.pry)
                sweep = cast(float, part.sweep)

                u = prx - arcCenter
                v = pry - arcCenter
                r = arcStart - arcCenter

                det = u.real*v.imag - u.imag*v.real
                alpha = (r.real*v.imag - r.imag*v.real) / det
                beta = (u.real*r.imag - u.imag*r.real) / det

                currentSegments.append(Arc(arcCenter, u, v, math.atan2(beta, alpha), sweep))
                current = part.end
            elif isinstance(part, svgelements.QuadraticBezier):
                currentSegments.append(QuadraticBezier(current, part.control, part.end))
                current = part.end
            elif isinstance(part, svgelements.CubicBezier):
                currentSegments.append(CubicBezier(current, part.control1, part.control2, part.end))
                current = part.end
            elif isinstance(part, svgelements.Close):
                currentSegments.append(Line(current, start))
                current = start
            else:
                print(f"Unknown path element: {type(part)} (part of {node.id})")
        finalizeSubpath()
        if temp.geometry: # skip paths with no drawable segments (e.g. d="" or only Move commands) -
            document.add(temp) # nothing to draw, and an empty Path would crash later in the pipeline
    elif isinstance(node, svgelements.Group):
        for child in node:
            parseSvgElement(child, docTransform, document, textNames)
    elif isinstance(node, svgelements.Text):
        if node.text: # only print for non empty text objects
            textNames.append(str(node.id))
    # isinstance() won't work on SVGElement because it encapsulates all other svg classes
    elif isinstance(node, svgelements.SVG) or type(node) == svgelements.svgelements.SVGElement:
        pass # these element types can be safely ignored because they are not geometry
    else:
        print(f"Ignored {type(node)} with name {node.id}")

# asks the user how to reconcile a viewport size that doesn't match the canvas size,
# returning the (scaleX, scaleY) factors to apply to the drawing.
def _promptRescale(svgWidth: float, svgHeight: float, canvasSize: complex) -> tuple[float, float]:
    canvasWidth, canvasHeight = canvasSize.real, canvasSize.imag
    if abs(svgWidth - canvasWidth) < 1e-6 and abs(svgHeight - canvasHeight) < 1e-6:
        return 1.0, 1.0

    fitWidth = canvasWidth / svgWidth
    fitHeight = canvasHeight / svgHeight

    print(f"\nCanvas is {canvasWidth:g} x {canvasHeight:g} mm; SVG viewport is {svgWidth:g} x {svgHeight:g} mm.")
    if abs(fitWidth - fitHeight) < 1e-6:
        answer = input("Keep drawing as-is (k), or rescale to fit the canvas (b)? ").strip().lower()
        if answer.startswith("b"):
            return fitWidth, fitWidth
        return 1.0, 1.0
    else:
        answer = input("Keep drawing as-is (k), rescale to fit width (x), rescale to fit height (y), or stretch to fill both axes (b)? ").strip().lower()
        if answer.startswith("x"):
            return fitWidth, fitWidth
        if answer.startswith("y"):
            return fitHeight, fitHeight
        if answer.startswith("b"):
            return fitWidth, fitHeight
        return 1.0, 1.0

# loads and parses the SVG file into an svgelements tree, re-raising any parse
# error as SvgParseError. Separate from parseSvg so the caller can load (and
# validate) the file, then ask the interactive rescale question, before starting
# the timer/profiler that only measures the computational parse below.
def loadSvg(svgPath: str) -> svgelements.SVG:
    try:
        svg = svgelements.SVG.parse(svgPath)
    except Exception as e:
        raise SvgParseError(f"Failed to parse SVG file '{svgPath}' ({e})") from e
    if svg.viewbox is None:
        raise SvgParseError(f"SVG file '{svgPath}' has no viewBox attribute; this converter requires one to determine the drawing's size")
    return svg

# asks the user how to reconcile the SVG viewport with the canvas (see _promptRescale).
# interactive, so kept out of parseSvg's timed/profiled body.
def promptRescale(svg: svgelements.SVG, settings: Settings) -> tuple[float, float]:
    assert svg.viewbox is not None # loadSvg already validated this
    svgWidth = cast(float, svg.viewbox.width)
    svgHeight = cast(float, svg.viewbox.height)
    return _promptRescale(svgWidth, svgHeight, settings.canvasSize)

def parseSvg(svg: svgelements.SVG, settings: Settings, scaleX: float, scaleY: float) -> Document:
    assert svg.viewbox is not None # loadSvg already validated this
    document = Document()
    transform = Transform()
    transform.scale(cast(float, svg.viewbox.height) / cast(float, svg.height)) # undo svgelements trying to scale document to viewport

    svgWidth = cast(float, svg.viewbox.width)
    svgHeight = cast(float, svg.viewbox.height)

    textNames: list[str] = []
    for child in svg:
        parseSvgElement(child, transform, document, textNames)

    # transform to printer space: scale per the rescale choice above, flip Y (svg is
    # top-down), then center the scaled viewport on the canvas and compensate for the
    # pen's offset from the nozzle
    scaledWidth, scaledHeight = scaleX * svgWidth, scaleY * svgHeight
    canvasCenter = settings.canvasOffset + settings.canvasSize / 2
    translateX = canvasCenter.real - scaledWidth / 2 - settings.penOffset.real
    translateY = canvasCenter.imag + scaledHeight / 2 - settings.penOffset.imag
    for path in document.objects:
        path.transform *= [scaleX, 0, 0, -scaleY, translateX, translateY]
        path.applyTransformations()
    if textNames:
        print(f"\nThis converter does not support text. In Inkscape, select the text and go to Path > Object to Path to convert it to lines this converter can draw. Text not included in the output gcode: {', '.join(textNames)}")
    return document
