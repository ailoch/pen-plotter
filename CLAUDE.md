# SVG-to-Gcode Pen Plotter

Converts an SVG drawing into G-code that drives a Bambu Lab P1S 3D printer as an XY pen plotter.

## Running

[`_Process.py`](_Process.py) is the entry point — prompts for SVG input and gcode output files, then runs:
1. **Parse** SVG → geometry model
2. **Infill** — add concentric loops to fill interiors of shapes
3. **Route** — reorder paths to minimize travel distance
4. **Generate** — converts intenal geometry model to G-code (lines → G1, arcs → G2/G3)

All logic lives in `lib/`, importable for testing (no module-level side effects).

## Geometry Model

[`lib/geometry.py`](lib/geometry.py): `Segment` (Line/Arc/QuadraticBezier/CubicBezier) → `Path` (list of segments, a subpath) → `PathObject` (list of Path + Style + Transform). `Document` = list of PathObjects.

Key: `Path.point(t)` spans whole subpath (0 ≤ t ≤ 1), `isClosed()`, `isFillable()` (encloses area, used to gate infill — separate from closed state), `tessellate(tolerance, allowArcs)` reduces to Line/Arc within tolerance, `rotateTo(index)` re-anchors closed paths (for routing).

## Key Pipeline Stages

**Parse** ([`lib/svgparse.py`](lib/svgparse.py)): SVG tree → `Document` of `PathObject`s. Handles groups, paths, rects, circles/ellipses; applies transform stack (flip Y for gcode, offset for pen geometry).

**Infill** ([`lib/infill.py`](lib/infill.py)): For each fillable `PathObject`, use pyclipper to generate concentric inward-offset loops respecting `fill-rule` (`nonzero`/`evenodd`). Offset the same pyclipper paths repeatedly to avoid compounding drift. Re-fit loop polylines with `fitLines=True` to recover circular arcs. Gated by `isFillable()` (encloses area, computed via shoelace formula), not `isClosed()`.

**Route** ([`lib/route.py`](lib/route.py)): TSP-like optimization (nearest-neighbor + 2-opt + anchor-optimization + rendezvous move) to reorder objects and choose entry points, minimizing pen travel. Two-pass: order internal subpaths within each multi-subpath object, then order objects themselves.

**Generate** ([`lib/plot.py`](lib/plot.py)): For each path, tessellate to Line/Arc, emit G1/G2/G3. Intersperse travel moves (short = pen-down, long = pen-up). Cosmetic: alternate +0.001mm Z per object for preview layering.

## Tessellation (`Path.tessellate`)

Bidirectional greedy fitter: reduces any curve to Line/Arc within tolerance, working in normalized subpath-space (0 ≤ t ≤ 1) so fits can span segment boundaries. Key behaviors:
- **Already-final segments atomic**: `Line` passthrough (unless `fitLines=True`), circular `Arc` passthrough → prformance gain.
- **Line merging**: `appendMerging` catches exact-collinear runs the fitter misses (each already fits individually).
- **`fitLines=True`** (used by infill arc-recovery): re-fits raw polygon points → arcs, with optimized fast path for all-`Line` ranges (no per-segment interior sampling).
- **Numerically unstable circumcircles rejected** via `MAX_RADIUS_TO_CHORD` guard — filters near-collinear noise.

## Settings (`settings.json`)

Loaded via `commentjson` (supports `//` comments). `machine` (startPos/penOffset/plateSize/drawableArea), `gcode` (per-state heights/speeds/accels, tessellationTolerance, infillSpacing), `visualization` (pen width, cosmetic layering/coloring for Bambu Studio preview), `debug` (showBoundingBoxes, profiling).

## Profiling

Set `debug.profiling: true` in `settings.json` to run under `cProfile` and print the 30 slowest functions by cumulative time.

## Hardware

**Bambu Lab P1S**: E-axis represents pen-tip distance (not filament), `M221 S0` disables extrusion. Pen offset from nozzle via `penOffset` (applied after SVG Y-flip). Renderer clips to `[-5000, 5256]`. Cosmetic `; FEATURE:` comments drive preview coloring.

## Dependencies

`pyclipper` (infill polygon offsetting), `svgelements` (SVG parsing), `commentjson` (JSONC), `scipy` (arc-length integration). `pyclipper` has no type stubs; `typings/pyclipper/__init__.pyi` is hand-cleaned via `stubgen`.

## Files

- `_Process.py` — entry point, calls other files
- `lib/geometry.py` — core geometry (Transform, Segment subclasses, Path, PathObject, Document, tessellation)
- `lib/plot.py` — gcode generation (State, PlotSettings, Plotter)
- `lib/svgparse.py` — SVG parsing
- `lib/route.py` — path ordering (TSP-like routing)
- `lib/infill.py` — concentric infill generation
- `settings.json` — machine/gcode/viz/debug config
- `typings/pyclipper/__init__.pyi` — type stubs for pyclipper
- `Append/{startCode,endCode}.gcode` — gcode templates
- `*.svg` — test drawings (`horse.svg` multi-subpath; `testDrawing.svg` tests fill rules)
