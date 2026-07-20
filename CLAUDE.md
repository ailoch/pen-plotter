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

**Parse** ([`lib/svgparse.py`](lib/svgparse.py)): SVG tree → `Document` of `PathObject`s. Handles groups, paths, rects, circles/ellipses; applies transform stack (flip Y for gcode, offset for pen geometry). If the SVG viewport size doesn't match `canvasSize`, `_promptRescale` asks the user how to reconcile it (see Alignment & Scaling below); the drawing is then centered on the canvas.

**Infill** ([`lib/infill.py`](lib/infill.py)): For each fillable `PathObject`, use pyclipper to generate concentric inward-offset loops respecting `fill-rule` (`nonzero`/`evenodd`). Offset the same pyclipper paths repeatedly (`_concentricLoops`) to avoid compounding drift. Re-fit loop polylines with `fitLines=True` to recover circular arcs. Gated by `isFillable()` (encloses area, computed via shoelace formula), not `isClosed()`. Then a **gap-fill pass** (`_gapFill`, on by default via `processing.generateGapInfill`) closes the two gaps concentric loops leave — acute-corner wedges and fractional-width slivers (where loops closing in from both sides meet with a residual `<spacing` gap). It's the slicer "gap infill" idea: stroke every drawn centerline (outline + all loops) by `spacing/2` via `ET_CLOSEDLINE` to get the covered region, `CT_DIFFERENCE` it against the fill region to get the uncovered residue, clean it (a morphological open at `tolerance/2` plus an `(spacing/2)²` min-area threshold — which also discards the benign sub-`penWidth` dots between adjacent loops, since `infillSpacing` is deliberately set under the real pen width so those dots are inked anyway), then fill each surviving piece — grouped with its holes via an `Execute2` PolyTree so annular gaps stay whole — with `_fillGap`, whose first inset halves from `spacing/2` down toward `tolerance/2` so a gap narrower than `spacing` still yields a centerline stroke. The drawn loops use `JT_ROUND`; the detection-only offsets (`_offsetPolys`) use the cheaper `JT_SQUARE` (negligible at nozzle scale). Coverage width is `infillSpacing` (the loop pitch), never `visualization.penWidth`.

**Route** ([`lib/route.py`](lib/route.py)): TSP-like optimization (nearest-neighbor + 2-opt + anchor-optimization + rendezvous move) to reorder objects and choose entry points, minimizing pen travel. Two-pass: order internal subpaths within each multi-subpath object, then order objects themselves.

**Generate** ([`lib/plot.py`](lib/plot.py)): For each path, tessellate to Line/Arc, emit G1/G2/G3. Intersperse travel moves (short = pen-down, long = pen-up). Cosmetic: alternate +0.001mm Z per object for preview layering.

## Tessellation (`Path.tessellate`)

Bidirectional greedy fitter: reduces any curve to Line/Arc within tolerance, working in normalized subpath-space (0 ≤ t ≤ 1) so fits can span segment boundaries. Key behaviors:
- **Already-final segments atomic**: `Line` passthrough (unless `fitLines=True`), circular `Arc` passthrough → prformance gain.
- **Line merging**: `appendMerging` catches exact-collinear runs the fitter misses (each already fits individually).
- **`fitLines=True`** (used by infill arc-recovery): re-fits raw polygon points → arcs, with optimized fast path for all-`Line` ranges (no per-segment interior sampling).
- **Numerically unstable circumcircles rejected** via `MAX_RADIUS_TO_CHORD` guard — filters near-collinear noise.

## Error Handling

**Missing/invalid config file** — `Settings.initFromJson` catches a missing file (`FileNotFoundError`) and parse errors (JSON syntax, type mismatches) separately, prints a clean one-line error message (e.g., `No terminal defined for 'f' at line 21 col 1`), and proceeds with defaults either way. The program continues so the user can still test with the default settings if needed.

**Invalid SVG file** — `parseSvg` wraps `svgelements` and re-raises any parse error as `SvgParseError`. `_Process.py`'s `run()` catches it around just the `parseSvg` call, prints it, and returns `False` instead of raising; the main loop re-prompts for a new file on `False` and retries. No config reload needed.

**Output file safety** — `lib/plot.py`'s `createFile` writes to a temp file in the output directory and only `os.replace()`s it over the real target on full success. If anything fails mid-pipeline (missing settings keys, missing prefix/suffix gcode files, etc.), the output file is left untouched — can't be truncated by a partial crash.

## Settings (`lib/settings.py`, `config/bambu_p1s_config.json`)

`Settings` (a dataclass, in `lib/settings.py` alongside the `State` enum it keys heights/speeds/accels/lineTypes by) is shared across the whole pipeline — `parseSvg`, `generateInfill`, `orderPaths`, and `createFile` all take a `Settings` instance and read the fields they need directly, rather than being passed individual values.

One config file per printer, named `config/<printer>_config.json` (currently just `bambu_p1s_config.json`). Loaded via `commentjson` (supports `//` comments). `machine` (startPos/penOffset/plateSize/safeZoneSize/safeZoneOffset/canvasSize/canvasOffset — see Alignment & Scaling below), `processing` (what is drawn on paper: tessellationTolerance, infillSpacing, prefix/suffix gcode template paths), `motion` (how the pen moves while drawing: per-state heights/speeds/accels, shortTravelThreshold, loadDelay), `visualization` (pen width, cosmetic layering/coloring for Bambu Studio preview), `debug` (showBoundingBoxes, optimizePathOrder, profiling). All fields are type-checked before use; on mismatch, the setting is skipped and a warning is printed. `initFromJson` ends with `_validateBounds()` (warn-and-continue, never resets to defaults — see Alignment & Scaling).

Positions (`endPos`, `penOffset`, `plateSize`, `safeZoneSize`, `safeZoneOffset`, `canvasSize`, `canvasOffset`) are stored as `complex`, matching how positions are represented everywhere else in the codebase — JSON's 2-element lists are converted via `complex(x, y)` in `initFromJson`. `startPos` is the one exception, kept as a `dict[str, float]` (`{"X":.., "Y":.., "Z":..}`) since it needs a Z component and `createFile`'s per-file `_DrawState.pos` (current nozzle position) is built directly from it.

## Alignment & Scaling (`lib/settings.py`, `lib/svgparse.py`)

All positions/sizes below are in mm, lower-left-corner convention, and nest: **plate ⊇ safe zone ⊇ canvas**.

- `plateSize` — the heatbed rect; its lower-left corner is fixed at the origin. This is the one physical/nozzle-space rect (the bed doesn't care about the pen).
- `safeZoneSize` / `safeZoneOffset` — the rect the pen can move within without colliding with anything. `safeZoneOffset` is in **pen space** (where the pen tip should be), not gcode/nozzle space.
- `canvasSize` / `canvasOffset` — the paper/drawable surface, also in pen space; `canvasSize` is the field users change to resize the paper.

Since gcode X/Y commands the nozzle, and the pen tip sits at `nozzle + penOffset`, a pen-space position `P` corresponds to nozzle position `N = P - penOffset`. `parseSvg`'s printer-space transform (`lib/svgparse.py`) builds its translation this way: it centers the drawing on `canvasOffset + canvasSize/2` (a pen-space point) and only then subtracts `penOffset` to get the nozzle-space translation actually written to gcode.

`Settings._validateBounds()` (called at the end of `initFromJson`) warns — but does **not** alter the loaded values or fall back to defaults — if any containment in the chain doesn't hold, checked in hierarchy order:
- safe zone not fully inside the plate — `safeZoneOffset` is already pen-space (i.e. already expressed in the same physical bed-frame numbers the plate rect uses), so this is a direct compare.
- canvas not fully inside the safe zone (both already pen-space, compared directly).
- nozzle movement not fully inside the plate — the nozzle's actual gcode movement, driving the pen across the safe zone, sits at `safeZoneOffset - penOffset`. A safe zone that keeps the pen on the plate can still walk the nozzle off it, or vice versa, so both are checked.

`parseSvg` always calls `_promptRescale(svgWidth, svgHeight, canvasSize)`, which returns `(1, 1)` with no prompt if the SVG viewport already matches `canvasSize`. Otherwise it prints both sizes and prompts for how to reconcile them: keep as-is, fit width, fit height, or stretch to fill both axes. When the canvas and viewport share an aspect ratio, fit-width/fit-height/stretch all reduce to the same scale, so the prompt collapses to just "keep as-is" vs. a single "rescale to fit" — using the same letter (`b`) as "stretch to fill both axes" in the general prompt, since they're the same operation in that case (helps muscle memory). The resulting scale is applied together with the Y-flip, canvas-centering, and `penOffset` compensation as one transform matrix per object (no separate steps).

## Profiling

Set `debug.profiling: true` in the config file to run under `cProfile` and print the 30 slowest functions by cumulative time.

## Hardware

**Bambu Lab P1S**: E-axis represents pen-tip distance (not filament), `M221 S0` disables extrusion. Pen offset from nozzle via `penOffset` (applied after SVG Y-flip). Renderer clips to `[-5000, 5256]`. Cosmetic `; FEATURE:` comments drive preview coloring.

`lib/plot.py`'s `BED_EXCLUDE_AREA` is built by `_bedExcludeArea` (plate rect minus the canvas rect) for the slicer to render the drawable area. The canvas is placed per `canvasOffset` — in pen space when `showPenPos` (the slicer applies `EXTRUDER_OFFSET = penOffset`), else shifted by `-penOffset` into nozzle space. Since both rects are axis-aligned, the shape is classified directly from which of the 4 plate edges (bottom/right/top/left) the canvas fails to touch ("gap" flags), no polygon library needed: 0 gaps → nothing excluded; all 4 → a ring (`_bridgeContours` keyholes the outer plate loop to the inner canvas hole); 2 opposite gaps → two disjoint strips (also `_bridgeContours`-joined); a contiguous run of 1-3 gaps → `_perimeterWalk` traces a single simple polygon (|/L/C) directly, no seam needed. `_removeRedundantPoints` then drops collinear midpoints from all cases. `safeZone` is not part of this polygon.

## Dependencies

`pyclipper` (infill polygon offsetting), `svgelements` (SVG parsing), `commentjson` (JSONC), `scipy` (arc-length integration). `pyclipper` has no type stubs; `typings/pyclipper/__init__.pyi` is hand-cleaned via `stubgen`.

## Files

- `_Process.py` — entry point & main loop (input prompts, retry logic, profiling wrapper)
- `lib/geometry.py` — core geometry (Transform, Segment subclasses, Path, PathObject, Document, tessellation)
- `lib/settings.py` — State enum, Settings dataclass, settings loader with error handling
- `lib/plot.py` — gcode generation (`createFile` and its helpers)
- `lib/svgparse.py` — SVG parsing (raises SvgParseError on invalid input)
- `lib/route.py` — path ordering (TSP-like routing)
- `lib/infill.py` — concentric infill generation
- `config/bambu_p1s_config.json` — machine/gcode/viz/debug config for the Bambu P1S; other printers get their own `config/<printer>_config.json`
- `typings/pyclipper/__init__.pyi` — type stubs for pyclipper
- `gcode_templates/bambu_p1s_{prefix,suffix}.gcode` — gcode templates referenced by the config's `prefixFile`/`suffixFile`; named per-printer like the config files
- `*.svg` — test drawings (`horse.svg` multi-subpath; `testDrawing.svg` tests fill rules)
