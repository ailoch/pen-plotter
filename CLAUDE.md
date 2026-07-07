# SVG-to-Gcode Pen Plotter

Converts an SVG drawing into G-code that drives a Bambu Lab P1S 3D printer as an XY pen
plotter (the extruder holds a pen instead of printing filament).

## Entry point

[`_Process.py`](_Process.py) is the whole program — a single script, run top-to-bottom
(no `if __name__ == "__main__"` guard, no CLI args yet):

```python
plotter = Plotter("settings.json")
document = parseSvg(fileIn, ...)
orderPaths(document, ...)  # reorder for minimal pen-up travel
plotter.createFile(document, fileOut)
input()  # keeps the console window open
```

`fileIn`/`fileOut` are hardcoded near the top of the file (`_Process.py:17-18`) —
currently `testDrawing.svg` -> `testDrawing.gcode`. Change these by hand to process a
different drawing (user wants to eventually prompt for these instead).

## Pipeline

1. **Parse** (`parseSvg` / `parseSvgElement`, `_Process.py:775` region "parseSvg")
   - Uses the `svgelements` library to parse the SVG.
   - Walks the SVG tree recursively (groups, paths, rects, circles/ellipses),
     converting each into a `PathObject` made of `Segment`s (`Line`, `Arc`,
     `QuadraticBezier`, `CubicBezier`).
   - Applies the SVG's own transform stack via the custom `Transform` class, then a
     final transform to flip Y (SVG +Y is down, gcode/printer +Y is up) and offset
     for the pen's position relative to the nozzle.
   - **Important `svgelements` gotcha:** `node.transform` is the transform already
     cascaded from the document root down to that node, not a local-only matrix —
     `parseSvgElement` must combine it with the constant `dimensions`/unit-correction
     transform directly (`transform @ Transform(node.transform)`), never accumulate it
     further through recursion, or nested groups' scale compounds with every level of
     nesting.
   - Text, defs, and other non-geometry nodes are ignored with a printed warning.

2. **Geometry model** (region "shapeDefs", `_Process.py:20-441`)
   - `Segment` (ABC) → `Line`, `Arc`, `QuadraticBezier`, `CubicBezier`. Each knows its
     own `length()`, `point(t)`, `derivative(t)`, `extrema()`, `bounds()`, and
     `tessellate(tolerance, maxDepth)` (returns a list of `Line`/circular-`Arc`
     segments approximating itself within `tolerance` mm — see "Adaptive
     tessellation" below).
   - `Path` = list of segments, with `start()`/`end()`, `isClosed()` (start ≈ end
     within a tolerance), `vertices()` (the point between every pair of consecutive
     segments — i.e. every candidate place the pen could enter/exit without changing
     the drawn shape; includes the final endpoint too for open paths),
     `rotateTo(index)` (re-splits a *closed* path so `segments[index]` is drawn
     first — raises if the path isn't closed, since re-splitting an open path would
     change its shape), and `tessellate(tolerance, maxDepth)` (non-mutating —
     concatenates each segment's own `tessellate()` into a new `Path`).
     `PathObject` = a `Path` + `Style` (stroke width/color, currently unused for
     output) + `Transform`.
   - `Document` = list of `PathObject`s plus an id→object lookup.
   - `Transform` is a hand-rolled 2D affine matrix (`[a,b,c,d,e,f]`, same convention as
     SVG's `matrix()`), supporting translate/scale/rotate/skew/flip and composition via
     `@`/`@=` (SVG-order) and `*`/`*=` (reverse order).

3. **Path ordering / routing** (`orderPaths`, region "routing", called between
   `parseSvg` and `plotter.createFile`)
   - Reorders `document.objects` (and reverses/rotates individual paths) to
     approximately minimize pen-up travel. This is TSP-like but not quite TSP: each
     path can be drawn forwards or backwards for free (2 candidate anchor points via
     `Path.reverse()`), and a *closed* path can additionally be entered/exited at any
     of its vertices (`Path.rotateTo()`), since it ends where it starts.
   - Solved with nearest-neighbor construction, then two interleaved local-search
     refinement passes run to convergence: 2-opt (try reversing runs of the tour —
     reversing a run also flips the draw direction of every open path in it, which
     leaves the connections *within* the run unchanged, so only the two boundary
     edges need comparing) and anchor optimization (for each closed path, try every
     vertex against its current tour neighbors, keep the cheapest).
   - Fast enough for the stated scale (200-300 objects, well under a second) without
     needing an external TSP/routing library.

4. **Gcode generation** (`Plotter` class, `_Process.py:517` region)
   - `addPath` first calls `objectGeo.tessellate(tessellationTolerance,
     maxTessellationDepth)` to reduce the path to only `Line`/circular-`Arc`
     segments (adaptive — see "Adaptive tessellation" below), then walks those:
     lines become `G1` moves, arcs become native `G2`/`G3` arcs. No other segment
     types can reach this point.
   - `penMove` decides travel vs. draw moves, and whether a travel move is "short"
     (stays down, `shortTravelThreshold`) or "long" (lifts pen to travel height,
     moves, lowers again).
   - `addLine` builds one gcode line, skipping redundant params (same speed/accel/
     position as last time), and can annotate lines with `; FEATURE: X` comments so
     Bambu Studio's preview colors moves by "line type" / "instruction" / "segment"
     (purely cosmetic — see `visualization.style` in settings).
   - `createFile` stitches together: `Append/startCode.gcode` (prefix) → generated
     moves for every object in the `Document` → `Append/endCode.gcode` (suffix).
     Prefix/suffix files use `{PLACEHOLDER}` tokens (e.g. `{TRAVEL_HEIGHT}`,
     `{LOAD_DELAY}`, `{BED_EXCLUDE_AREA}`) substituted from `PlotSettings` via
     `fileAppend`.

### Adaptive tessellation (`Segment.tessellate`/`Path.tessellate`, used by `addPath`)

Reduces any path to only `Line`s and circular `Arc`s (gcode's native `G1`/`G2`/`G3`
primitives), fit to within `tessellationTolerance` mm of the original curve instead of
chopping into fixed-length pieces — long gentle curves get few segments, only sharp
bends get many.

- `Line.tessellate` → itself (already exact). `Arc.tessellate` → itself if
  `abs(abs(u)-abs(v)) <= tolerance` (already circular — same tolerance value doubles as
  the "is this basically a circle" threshold); otherwise, like `QuadraticBezier`/
  `CubicBezier`, delegates to the shared `Segment._fitToTolerance` method.
- `Segment._fitToTolerance(t0, t1, tolerance, maxDepth, depth)` recursively fits the
  cheapest option first: (1) a `Line` from `point(t0)` to `point(t1)`, accepted if a
  handful of interior sample points deviate from the chord by no more than tolerance;
  (2) else a circular `Arc` via `Arc.fromThreePoints(point(t0), point(mid), point(t1))`
  (an alternate constructor — circumcircle for center/radius, then angles around that
  center to pick `t0`/`sweep)`, returns`None` when the 3 points are ~collinear;
  (3) else split at the midpoint and recurse on each half.
  `maxTessellationDepth` caps the recursion as a safety net for pathological curves
  (cusps, coincident control points) — hitting it falls back to the Line from step 1
  and prints a warning rather than raising.
- `_fitToTolerance` is defined once on the `Segment` base class (not per subclass)
  because it only calls `self.point(t)`, which every segment type provides and which
  is valid for any real `t`, not just `[0,1]`.
- `Path.tessellate` is a thin, non-mutating aggregator — concatenates each segment's
  `tessellate()` into a new `Path`, leaving the original untouched (other code, like the
  debug bounding boxes, still reads the original geometry).

## Settings (`settings.json`, loaded via `commentjson` so `//` comments are allowed)

Loaded into `PlotSettings` (`_Process.py:443`) by `initFromJson`, which validates
setting names against the dataclass fields and prints a warning for anything unknown
(no full type-checking yet — see `#TODO`).

- `machine`: `startPos` (nozzle X/Y/Z home), `penOffset` (pen vs. nozzle, since the pen
  is mounted offset from where the nozzle would be), `plateSize` (256x256 for P1S),
  `drawableArea` (safe region the pen can reach on the paper).
- `gcode`: per-`State` (`draw`/`travel`, plus debug bounding-box states) heights/
  speeds/accels, `shortTravelThreshold`, `tessellationTolerance`/
  `maxTessellationDepth` (see "Adaptive tessellation" above), and the prefix/suffix
  gcode file paths.
- `visualization`: cosmetic-only settings controlling how the gcode looks in Bambu
  Studio's preview (pen width for line rendering, `lineTypes` per state, `loadDelay`
  seconds to wait for the user to load the pen, `showPenPos` toggle, and the
  `style`/`styleLineOrder` coloring scheme described in the comments in
  `_Process.py:568-588`).
- `debug`: `showBoundingBoxes` — draws segment/path/document bounding rectangles in
  the output for visual debugging (as `_SEGMENT_BOUNDS`/`_PATH_BOUNDS`/
  `_DOCUMENT_BOUNDS` pseudo-states).

## Hardware notes (why the code looks the way it does)

- Printer is a **Bambu Lab P1S** driven as a plotter: the "extruder" (E axis) doesn't
  push filament, it represents **pen-tip distance traveled** (see commit
  "Change e axis to be dist moved" — `E` in `penMove` is `hypot(dx, dy)`, not a
  filament amount). `M221 S0` in the prefix gcode disables real extrusion.
- Pen is mounted with an offset from the nozzle (`penOffset`), so all geometry is
  shifted after the SVG's own Y-flip.
- Bambu Studio's renderer breaks on very large coordinates, so `Segment.bounds()`
  clamps to `[-5000, 5256]` (`_Process.py:172`).
- `Append/startCode.gcode` / `endCode.gcode` are real Bambu Studio start/end gcode
  (home, bed leveling skip, fan/temp no-ops, motor current tweaks) with placeholders
  swapped in; they were renamed from `startcode`/`endcode` because `commentjson`
  choked on special characters when they were inline in the JSON.
- `; FEATURE: X` comments are how Bambu Studio's slicer preview assigns per-move
  colors — hijacked here purely for visualizing plot order/type, not for anything
  print-quality related.

## Known TODOs / rough edges (from comments in the code)

- `fileIn`/`fileOut` should eventually be user-prompted instead of hardcoded.
- Color (`strokeColor`/`fillColor` hex→RGB) isn't implemented; `Style` colors are
  unused placeholders.
- `Document.add()` has a noted bug: adding an object whose `id` collides with an
  existing one silently overwrites the `id` lookup without warning.
- The print-area scale line in `parseSvg` (`#transform.scale(dimensions.imag / svg.height)`)
  is still commented out — nothing currently scales the drawing to fit `drawableArea`;
  the SVG is plotted at its authored physical size (mm).
- No type-checking of values loaded from `settings.json` yet.

## Files

- `_Process.py` — the whole pipeline (parse SVG → geometry model → gcode).
- `settings.json` — machine/gcode/visualization/debug config, loaded at the bottom of
  `_Process.py`.
- `Append/startCode.gcode`, `Append/endCode.gcode` — prefix/suffix gcode templates.
- `*.svg` (`horse.svg`, `test2.svg`) — sample input drawings.
- `*.gcode` (`horse.gcode`, `testDrawing.gcode`) — generated output, not source of truth.
