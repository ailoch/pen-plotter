import math
import svgelements

from lib.geometry import Style, Transform, Segment, Line, Arc, QuadraticBezier, CubicBezier, Path, PathObject, Document

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

def parseSvgElement(node: svgelements.SVGElement, docTransform: Transform, document: Document):
    nodeTransform = docTransform @ Transform(getattr(node, "transform", None))
    if isinstance(node, svgelements.Rect):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = nodeTransform

        # pylance seems to think node.x and node.y are None (they are actually floats)
        xmin = node.x
        xmax = node.x + node.width # type: ignore
        ymin = node.y * 1j # type: ignore
        ymax = (node.y + node.height) * 1j # type: ignore
        temp += Line(xmin+ymin, xmin+ymax)
        temp += Line(xmin+ymax, xmax+ymax)
        temp += Line(xmax+ymax, xmax+ymin)
        temp += Line(xmax+ymin, xmin+ymin)
        document.add(temp)
    elif isinstance(node, (svgelements.Circle, svgelements.Ellipse)):
        temp = PathObject(str(node.id)) # str() to make pylance happy
        temp.style = readStyle(node)
        temp.transform = nodeTransform

        center = node.cx + node.cy*1j # type: ignore
        temp += Arc(center, node.rx, node.ry * 1j) # type: ignore
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
                u = part.prx - part.center # type: ignore
                v = part.pry - part.center # type: ignore
                r = part.start - part.center

                det = u.real*v.imag - u.imag*v.real # type: ignore
                alpha = (r.real*v.imag - r.imag*v.real) / det # type: ignore
                beta = (u.real*r.imag - u.imag*r.real) / det # type: ignore

                currentSegments.append(Arc(part.center, u, v, math.atan2(beta, alpha), part.sweep)) # type: ignore
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
        if not temp.geometry: # check for empty paths
            temp.geometry = [Path()]
        document.add(temp)
    elif isinstance(node, svgelements.Group):
        for child in node:
            parseSvgElement(child, docTransform, document)
    # isinstance() won't work on SVGElement because it encapsulates all other svg classes
    elif isinstance(node, svgelements.SVG) or type(node) == svgelements.svgelements.SVGElement:
        pass # these element types can be safely ignored because they are not geometry
    else:
        print(f"Ignored {type(node)} with name {node.id}")

def parseSvg(svgPath: str, dimensions: complex, offset: complex) -> Document:
    document = Document()
    svg = svgelements.SVG.parse(svgPath)
    transform = Transform()
    #TODO: add warning when document height and width don't match
    # FIXME: scaling logic can be weird sometimes
    transform.scale(svg.viewbox.height / svg.height) # undo svgelements trying to scale document to viewport

    # this line can cause unexpected behavior sometimes
    # mabye ask user if they want to scale drawing?
    #transform.scale(dimensions.imag / svg.viewbox.height) # scale to print area

    for child in svg:
        parseSvgElement(child, transform, document)
    for path in document.objects:
        # transform to printer space
        path.transform *= [1, 0, 0, -1, -offset.real, 256-offset.imag]
        path.applyTransformations()
    return document
