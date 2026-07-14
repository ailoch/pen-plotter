from lib.geometry import Path, Document

try:
    import pyclipper
except ImportError:
    pyclipper = None

_SCALE = 1e5 # pyclipper needs integer coordinates; this gives ~10nm precision at mm scale

def _toClipperPath(points: list[complex]) -> list[tuple[int, int]]:
    return [(round(p.real * _SCALE), round(p.imag * _SCALE)) for p in points]

def _fromClipperPath(path) -> list[complex]:
    return [complex(x / _SCALE, y / _SCALE) for x, y in path]

# generates concentric infill loops for every PathObject with a set fill color,
# appending them as new subpaths to object.geometry (the original perimeter
# subpaths are left untouched, and drawn first - see Plotter.addPath). runs in
# printer space (mm), so must be called after parseSvg's transforms are applied.
# spacing <= 0 disables infill entirely
def generateInfill(document: Document, spacing: float, tolerance: float, maxDepth: int):
    if spacing <= 0:
        return
    if pyclipper is None:
        print("Warning: pyclipper is not installed (pip install pyclipper); skipping infill generation")
        return

    for obj in document.objects:
        if obj.style.fillColor is None:
            continue

        fillableSubpaths = [p for p in obj.geometry if p.isFillable()]
        if not fillableSubpaths:
            continue

        # tessellate with allowArcs=False so every subpath is flattened to Lines
        # only (all pyclipper understands) - Path.vertices() then gives the
        # polygon points directly, correctly including the true final point for
        # open paths (relying on SVG's implicit fill closure back to the start)
        clipperPaths = [_toClipperPath(p.tessellate(tolerance, maxDepth, allowArcs=False).vertices()) for p in fillableSubpaths]
        clipperPaths = [p for p in clipperPaths if len(p) >= 3]
        if not clipperPaths:
            continue

        fillType = pyclipper.PFT_EVENODD if obj.style.fillRule == "evenodd" else pyclipper.PFT_NONZERO

        try:
            pc = pyclipper.Pyclipper()
            pc.AddPaths(clipperPaths, pyclipper.PT_SUBJECT, True)
            region = pc.Execute(pyclipper.CT_UNION, fillType, fillType)
        except pyclipper.ClipperException as e:
            print(f"Warning: pyclipper failed to resolve fill region for object {obj.id!r} ({e}); skipping infill for it")
            continue
        if not region:
            continue

        # offset repeatedly from the ORIGINAL resolved region (not chained from the
        # previous loop) to avoid compounding discretization drift - each Execute
        # call recomputes fresh from the paths added below, just with a larger
        # cumulative delta
        pco = pyclipper.PyclipperOffset()
        # how finely JT_ROUND flattens its fillet arcs. pyclipper's default
        # (0.25 scaled units ~ 2.5nm here) is far finer than tolerance needs and
        # floods each loop with points that tessellate() then has to re-fit;
        # tol/4 keeps fillet deviation negligible while cutting the point count
        # (and infill time) several-fold
        pco.ArcTolerance = tolerance / 4 * _SCALE
        pco.AddPaths(region, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)

        loops = []
        step = 1
        maxSteps = 10000 # safety net against a pathological infinite loop
        while step <= maxSteps:
            try:
                offsetResult = pco.Execute(-spacing * step * _SCALE)
            except pyclipper.ClipperException as e:
                print(f"Warning: pyclipper offset failed for object {obj.id!r} at step {step} ({e}); stopping infill for it")
                break
            if not offsetResult:
                break
            loops.extend(offsetResult)
            step += 1

        for loopPts in loops:
            realPts = _fromClipperPath(loopPts)
            if len(realPts) < 3:
                continue
            loop = Path.fromPoints(realPts, closed=True)
            obj.geometry.append(loop.tessellate(tolerance, maxDepth, fitLines=True))
