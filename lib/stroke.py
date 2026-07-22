import copy, math
from lib.geometry import Document
from lib.settings import LineType, Settings
from lib.infill import _SCALE, _appendLoop, _drawArcTolerance, _joinType, _toClipperPath

try:
    import pyclipper
except ImportError:
    pyclipper = None

# offsets from the centerline (in mm) for the passes making up one side of a stroke,
# not counting a center pass. numPasses is the total conceptual pass count (center +
# both sides, for a closed/two-sided stroke); s is the pitch between adjacent passes.
# odd numPasses always includes a center pass at delta=0 (handled separately by the
# caller) with rings spaced every s out to the same outer edge; even numPasses has no
# center pass, so its innermost ring sits at s/2 instead of s so the pitch between the
# two innermost rings (one on each side) still comes out to s. either way the outermost
# delta + s/2 lands exactly at strokeWidth/2 - see CLAUDE.md's Stroke section.
def _passDeltas(numPasses: int, s: float) -> list[float]:
    if numPasses % 2 == 1:
        return [k * s for k in range(1, (numPasses - 1) // 2 + 1)]
    return [(k - 0.5) * s for k in range(1, numPasses // 2 + 1)]

# generates the multi-pass concentric strokes
# for every PathObject with a set stroke color, appending them as new STROKE-tagged
# subpaths to object.geometry. runs in printer space (mm).
# settings.generateStroke=False (or a missing pyclipper install) disables the multi-pass
# expansion and draws a single centerline STROKE pass per subpath instead
def generateStroke(document: Document, settings: Settings):
    spacing = settings.fillSpacing
    tolerance = settings.tessellationTolerance
    warnedMissingPyclipper = False

    for obj in document.objects:
        style = obj.style
        if style.strokeColor is None or style.strokeWidth <= 0:
            continue

        # only the raw, un-stroked/un-filled centerline geometry is a stroke source -
        # this excludes any loops a previous pass already generated
        rawSubpaths = [p for p in obj.geometry if p.lineType == LineType.RAW_GEOMETRY]
        if not rawSubpaths:
            continue

        # settings.generateStroke=False opts out of multi-pass expansion entirely,
        # same fallback as a missing pyclipper install - draws the raw centerline
        # directly as a single STROKE pass (the pre-multi-pass behavior)
        if not settings.generateStroke or pyclipper is None:
            if pyclipper is None and not warnedMissingPyclipper:
                print("Warning: pyclipper is not installed (pip install pyclipper); drawing strokes as a single centerline pass")
                warnedMissingPyclipper = True
            for p in rawSubpaths:
                centerPass = copy.deepcopy(p)
                centerPass.lineType = LineType.STROKE
                obj.geometry.append(centerPass)
            continue

        numPasses = max(1, math.ceil(style.strokeWidth / spacing)) if spacing > 0 else 1
        s = style.strokeWidth / numPasses
        centerPassNeeded = numPasses % 2 == 1

        deltas = _passDeltas(numPasses, s)
        joinType = _joinType(style.linejoin)

        for p in rawSubpaths:
            if centerPassNeeded:
                centerPass = copy.deepcopy(p)
                centerPass.lineType = LineType.STROKE
                obj.geometry.append(centerPass)

            if not deltas:
                continue

            # closed paths omit the duplicate end point, open ones include it - both
            # match pyclipper's expectation for AddPath
            closed = p.isClosed()
            vertices = p.tessellate(tolerance, allowArcs=False).vertices()
            clipperPath = _toClipperPath(vertices)
            if len(clipperPath) < 2:
                continue

            # closed -> ET_CLOSEDLINE; open -> pyclipper's open-line end type per style.linecap
            endType = pyclipper.ET_CLOSEDLINE if closed else {"round": pyclipper.ET_OPENROUND, "square": pyclipper.ET_OPENSQUARE}.get(style.linecap, pyclipper.ET_OPENBUTT) # default / "butt"

            for delta in deltas:
                pco = pyclipper.PyclipperOffset()
                pco.ArcTolerance = _drawArcTolerance(tolerance)
                pco.MiterLimit = style.miterlimit
                pco.AddPath(clipperPath, joinType, endType)
                try:
                    # positive delta grows outward from the centerline - a closed
                    # path's ET_CLOSEDLINE offset yields both the outer ring and the
                    # inner hole in one Execute call; an open path's yields the single
                    # contour wrapping both sides plus caps
                    result = pco.Execute(delta * _SCALE)
                except pyclipper.ClipperException as e:
                    print(f"Warning: pyclipper stroke offset failed for object {obj.id!r} at delta {delta:g}mm ({e}); skipping this pass")
                    continue
                for contour in result:
                    _appendLoop(obj.geometry, contour, LineType.STROKE, tolerance)

# removes RAW_GEOMETRY paths from every object's geometry (they've served their
# purpose as a stroke/fill source and would otherwise confuse the router - a path
# that's never drawn shouldn't factor into travel-distance optimization), then drops
# any object left with no geometry at all
def dropRawGeometry(document: Document):
    survivors = []
    for obj in document.objects:
        obj.geometry = [p for p in obj.geometry if p.lineType != LineType.RAW_GEOMETRY]
        if obj.geometry:
            survivors.append(obj)
        elif document.id.get(obj.id) is obj: # guards against Document.add's known id-collision edge case
            del document.id[obj.id]
    document.objects = survivors
