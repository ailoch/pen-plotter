# SVG-to-Gcode Pen Plotter

Converts an SVG drawing into G-code that drives a Bambu Lab P1S 3D printer as an XY pen
plotter (the extruder holds a pen instead of printing filament).

## Entry point

[`_Process.py`](_Process.py) is the whole program — a single script, run top-to-bottom
(no `if __name__ == "__main__"` guard, no CLI args yet):

```python
plotter = Plotter("settings.json")
document = parseSvg(fileIn, ...)
plotter.createFile(document, fileOut)
input()  # keeps the console window open
```

`fileIn`/`fileOut` are hardcoded near the top of the file (`_Process.py:17-18`) —
currently `test2.svg` -> `testDrawing.gcode`. Change these by hand to process a
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
   - **Important `svgelements` gotcha (see "Fixed bugs" below):** `node.transform` is
     the transform already cascaded from the document root down to that node, not a
     local-only matrix — `parseSvgElement` must combine it with the constant
     `dimensions`/unit-correction transform directly (`transform @ Transform(node.transform)`),
     never accumulate it further through recursion, or nested groups' scale compounds
     with every level of nesting.
   - Text, defs, and other non-geometry nodes are ignored with a printed warning.

2. **Geometry model** (region "shapeDefs", `_Process.py:20-441`)
   - `Segment` (ABC) → `Line`, `Arc`, `QuadraticBezier`, `CubicBezier`. Each knows its
     own `length()`, `point(t)`, `derivative(t)`, `extrema()`, `bounds()`.
   - `Path` = list of segments. `PathObject` = a `Path` + `Style` (stroke width/color,
     currently unused for output) + `Transform`.
   - `Document` = list of `PathObject`s plus an id→object lookup.
   - `Transform` is a hand-rolled 2D affine matrix (`[a,b,c,d,e,f]`, same convention as
     SVG's `matrix()`), supporting translate/scale/rotate/skew/flip and composition via
     `@`/`@=` (SVG-order) and `*`/`*=` (reverse order).

3. **Gcode generation** (`Plotter` class, `_Process.py:517` region)
   - `addPath` walks each segment: lines are drawn directly; perfect-radius arcs
     become native `G2`/`G3` arcs; non-circular arcs and beziers are tessellated into
     line segments (`tesselate`, fixed segment length, not adaptive yet — see TODO).
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

## Settings (`settings.json`, loaded via `commentjson` so `//` comments are allowed)

Loaded into `PlotSettings` (`_Process.py:443`) by `initFromJson`, which validates
setting names against the dataclass fields and prints a warning for anything unknown
(no full type-checking yet — see `#TODO`).

- `machine`: `startPos` (nozzle X/Y/Z home), `penOffset` (pen vs. nozzle, since the pen
  is mounted offset from where the nozzle would be), `plateSize` (256x256 for P1S),
  `drawableArea` (safe region the pen can reach on the paper).
- `gcode`: per-`State` (`draw`/`travel`, plus debug bounding-box states) heights/
  speeds/accels, `shortTravelThreshold`, `avgTesselatedLineLength`, and the
  prefix/suffix gcode file paths.
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
- `Path.tessellate()` is an empty stub; actual tessellation happens ad hoc in
  `Plotter.tesselate()` and is fixed-length, not adaptive.
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
