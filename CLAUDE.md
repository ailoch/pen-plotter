# SVG-to-Gcode Pen Plotter

Converts an SVG drawing into G-code that drives a Bambu Lab P1S 3D printer as an XY pen
plotter (the extruder holds a pen instead of printing filament).

## Entry point

[`_Process.py`](_Process.py) is a thin run script, executed top-to-bottom (no
`if __name__ == "__main__"` guard, no CLI args yet) — the underscore keeps it sorted to
the top of the file explorer. It just imports from [`lib/`](lib/) (see "Files" below)
and runs the pipeline:

```python
plotter = Plotter("settings.json")
document = parseSvg(fileIn, ...)
generateInfill(document, ...)  # add concentric infill loops to filled shapes
orderPaths(document, ...)  # reorder for minimal pen-up travel
plotter.createFile(document, fileOut)
input()  # keeps the console window open
```

`fileIn`/`fileOut` are hardcoded in `_Process.py` itself — currently `testDrawing.svg`
-> `testDrawing.gcode`. Change these by hand to process a different drawing (user wants
to eventually prompt for these instead).

All the actual logic lives in `lib/`, a plain import-safe package (no module-level
execution) — `import lib.route` etc. works with no side effects, which is how this
project's ad-hoc tests exercise the pipeline (parse → route → generate gcode) without
triggering a real run.

## Pipeline

1. **Parse** (`parseSvg` / `parseSvgElement`, [`lib/svgparse.py`](lib/svgparse.py))
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
   - A `<path>`'s `d` attribute can encode a *compound* shape as multiple `M`/`m`
     (moveto) subpaths in one element (e.g. a shape with a hole, or several
     unconnected strokes that are still logically one drawn object) — `horse.svg`'s
     single `<path>` is a real example, with 3 subpaths. `parseSvgElement` finalizes
     the segments collected so far into their own `Path` on every `Move` after the
     first, populating `PathObject.geometry` with one entry per subpath (previously
     all subpaths were merged into one continuous `Path`, which drew a spurious line
     connecting unrelated loops).
   - `readStyle` reads `stroke_width` plus, via svgelements' raw `element.values`
     dict (which resolves inherited/cascaded properties — e.g. `horse.svg`'s fill is
     set on the root `<svg>`, not the path itself, and still resolves correctly),
     fill presence (`values.get("fill") == "none"` ⇒ `Style.fillColor = None`) and
     fill-rule (`values.get("fill-rule", "nonzero")`) — see "Infill generation" below.
   - Text, defs, and other non-geometry nodes are ignored with a printed warning.
   - **`svgelements` has no real type stubs.** Unlike `pyclipper` (a compiled
     extension with *no* source for Pylance to inspect at all), `svgelements` is
     pure Python, so Pylance tries to infer attribute types directly from its
     source — but that source initializes attributes like `Rect.x`/`Circle.cx`/
     `Arc.center` to `None` in `__init__` and then reassigns them through several
     untyped, dynamic code paths (`property_by_object`, `property_by_values`,
     etc.), so Pylance lands on a messy `Unknown | None` for nearly every
     geometric attribute. A hand-written `.pyi` stub isn't practical here (the
     library is ~9,700 lines across ~40 classes). Instead, `parseSvgElement`
     wraps each such access in `typing.cast()` (e.g. `cast(float, node.x)`,
     `cast(complex, part.center)`) — a zero-runtime-cost, type-checker-only
     annotation that documents the true type without swallowing errors on the
     rest of the line the way a blanket `# type: ignore` would. The `complex`
     casts rely on svgelements' own `Point` class implementing the same
     `.real`/`.imag`/arithmetic protocol `complex` does (by its own docstring,
     as a drop-in replacement) — this codebase already relied on that duck
     typing before the casts existed; `cast()` just makes it explicit instead of
     silently smuggling a `Point` through as `complex`.

2. **Geometry model** ([`lib/geometry.py`](lib/geometry.py))
   - `Segment` (ABC) → `Line`, `Arc`, `QuadraticBezier`, `CubicBezier`. Each knows its
     own `length()`, `point(t)`, `derivative(t)`, `extrema()`, `bounds()`. Tessellation
     (reducing to `Line`/circular-`Arc`) is NOT a per-segment concern any more — see
     `Path.tessellate` and "Adaptive tessellation" below. `Arc` additionally has
     `toPoints(tolerance)`, which samples itself into points fine enough that none
     deviates from the true circle by more than `tolerance` (a fixed angular step
     derived from the chord's sagitta) — used by infill generation to flatten arcs
     for pyclipper (which only understands straight polygons).
   - `Style` = `strokeWidth` (unused for output) + `fillColor` (`None` means SVG
     `fill:none` — no infill; a color, currently just presence not the actual RGB,
     means filled) + `fillRule` (`"nonzero"` or `"evenodd"`, read from the SVG
     `fill-rule` property — see "Infill generation" below).
   - `Path` = list of segments, with `start()`/`end()`, `point(t)` (a single
     normalized parameter, `0 <= t <= 1`, spanning the *entire* subpath — resolves
     which segment `t` falls into internally; the building block "Adaptive
     tessellation" below fits across), `isClosed()` (start ≈ end within a
     tolerance), `isFillable()` (encloses non-zero area — deliberately *separate*
     from `isClosed()`: an open path can still enclose area via SVG's implicit fill
     closure, and a closed path can enclose zero area, e.g. a degenerate
     out-and-back trace; computed via the shoelace formula over each segment
     sampled at several points, not raw `vertices()`, so a full-circle `Arc` —
     whose start and end coincide — is measured correctly instead of collapsing to
     a single point), `vertices()` (the point between every pair of consecutive
     segments — i.e. every candidate place the pen could enter/exit without changing
     the drawn shape; includes the final endpoint too for open paths),
     `rotateTo(index)` (re-splits a *closed* path so `segments[index]` is drawn
     first — raises if the path isn't closed, since re-splitting an open path would
     change its shape), `tessellate(tolerance, maxDepth, allowArcs=True)` (see
     "Adaptive tessellation" below), and the classmethod `fromPoints(points,
     closed=False)` (builds a single-subpath `Path` of `Line`s connecting the given
     points, closing back to the first if `closed`).
     `PathObject` = a `list[Path]` + `Style` (fill presence/rule drive infill; stroke
     width/color still unused for output) + `Transform`. Almost always one `Path`
     (one `<rect>`/`<circle>`/`<ellipse>`, or one simple `<path>`), but more than
     one for a compound `<path>`
     (see the parsing gotcha above) — all subpaths of one `PathObject` are
     considered part of the same logical object for routing/infill purposes.
     `PathObject` mirrors `Path`'s own interface (`start()`/`end()`/`isClosed()`/
     `vertices()`/`rotateTo()`/`reverse()`/`bounds()`) so the routing code in
     `orderPaths` can treat a `list[Path]` and a `list[PathObject]` identically (see
     "Path ordering / routing" below) — `isClosed()` is `True` only when there's
     exactly *one* subpath and it's closed; `vertices()`/`rotateTo()` raise
     `ValueError` otherwise (same invariant `Path.rotateTo()` already enforces). A
     multi-subpath (or single open-path) object is otherwise treated like an open
     `Path`: freely reversible as a whole, but not re-anchorable at an arbitrary
     vertex. `point(subpathIndex, t)` mirrors `Path.point(t)` for a specific subpath.
   - `Document` = list of `PathObject`s plus an id→object lookup.
   - `Transform` is a hand-rolled 2D affine matrix (`[a,b,c,d,e,f]`, same convention as
     SVG's `matrix()`), supporting translate/scale/rotate/skew/flip and composition via
     `@`/`@=` (SVG-order) and `*`/`*=` (reverse order).

3. **Infill generation** (`generateInfill`, [`lib/infill.py`](lib/infill.py), called
   right after `parseSvg`, before routing — requires `pip install pyclipper`)
   - For every `PathObject` with `style.fillColor is not None`, generates nested
     inward-offset loops filling the interior and appends them to `object.geometry`
     (the original perimeter subpaths are left untouched and drawn first). Runs in
     printer space (mm), after `parseSvg`'s transforms are already applied, so
     offsets are physically uniform regardless of the SVG's own transform (rotation/
     shear/etc.).
   - Uses **pyclipper** (Clipper) rather than `shapely` (also installed) because it
     natively resolves self-intersections and multiple fill regions under *both*
     `nonzero` and `evenodd` fill rules, and handles holes/multi-region shapes and
     offsetting in one tool.
   - Pipeline per object: flatten every `isFillable()` subpath to an integer-scaled
     polygon — `path.tessellate(tolerance, maxDepth, allowArcs=False).vertices()`
     fully flattens every segment (including circular `Arc`s, via `Arc.toPoints()`)
     to straight-line points and already includes the correct final point for open
     paths (see `Path.vertices()`/`isClosed()` above), then `_toClipperPath` scales
     to integers (pyclipper needs them; a fixed `_SCALE = 1e5` gives ~10nm precision
     at mm scale); `Pyclipper().Execute(CT_UNION, ...)` with the object's `fillRule`
     resolves self-intersections/holes/multiple regions into one clean boundary;
     then `PyclipperOffset` is `Execute`'d repeatedly with an increasing cumulative
     delta (`-spacing`, `-2*spacing`, ...) **from the same originally-added paths
     each time** (not chained loop-to-loop) to avoid compounding discretization
     drift, until a call returns empty. Every polygon returned by every step becomes
     one infill loop (each `Execute` may return several — the same call is what
     naturally splits/grows holes as area shrinks). Uses `JT_ROUND` joins (rounds
     gaps at reflex/concave corners created by the inward offset) — a deliberate
     aesthetic choice, not load-bearing for correctness: an earlier suspicion that
     `JT_ROUND` caused spurious arcs on `horse.svg`'s legs turned out to be wrong
     (see "Arc recovery" below).
   - **Arc recovery**: pyclipper's offset output is plain polylines, so
     `_fitPointsToTolerance`/`_loopToPath` (via the shared `_tryFit` helper) re-fit
     each loop back to `Line`/`Arc` segments (a discrete-point-list analog of
     `Path`'s own bidirectional fitter — see "Adaptive tessellation" below) so a
     circular fill's loops come back out as a couple of `G2`/`G3` arcs instead of a
     many-sided polygon of `G1`s. Corners must be detected first
     (`_findCornerIndices`, turning angle > `_CORNER_ANGLE_THRESHOLD`) and never
     fit across, since any 3 non-collinear points define *some* circumcircle, and a
     large-radius one can closely hug two straight edges meeting at a gentle angle
     if corners aren't excluded first (found via `testDrawing.svg`'s `triangle`,
     which has 3 sharp corners plus one genuinely curved edge — a good regression
     case for this). `Path`'s own fitter hits this exact same bug class in a
     different form (see `MAX_RADIUS_TO_CHORD` in "Adaptive tessellation" below) —
     `Arc.fromThreePoints`' circumcircle math is fundamentally numerically unstable
     for near-collinear inputs regardless of which fitter calls it.
   - **`_tryFit`'s midpoint safety check**: a candidate Line/Arc is validated not
     just against the points pyclipper actually gave it, but also against the
     midpoint of every *consecutive* pair of those points. pyclipper always
     connects consecutive points with a literal straight sub-edge (a polygon's
     straight edges are only ever given as their two sparse endpoints; curves are
     represented by densely sampling many points, never by hidden curvature inside
     one edge), so this check is free (always valid) and catches a real bug: a
     long, sparsely-sampled straight edge next to a tiny, densely-sampled
     `JT_ROUND` fillet at a reflex corner gave the old deviation checks nothing to
     fail on — a circumcircle threading through the two far polygon vertices and
     the fillet cluster could satisfy tolerance against every *given* point while
     still cutting several mm across the middle of what should be a straight edge.
     Confirmed empirically on `testDrawing.svg`'s `nonzeroFill`: a spurious
     18.8mm-radius arc deviated 2mm from the true edge at its unsampled midpoint.
   - **`_fitPointsToTolerance`'s galloping search**: greedily consumes the largest
     prefix of the remaining points that still fits a single Line/Arc, rather than
     blindly bisecting the remaining range in half whenever the whole thing
     doesn't fit. For a curve with genuinely varying curvature (e.g. an ellipse —
     unlike a circle, where any 3 points define exactly the right circle), naive
     halving converges to many tiny `Line`s well before it reaches the true
     tolerance-limited extent of a good `Arc`, since each half that still fails
     just gets bisected again regardless of where the real boundary is (this made
     `filledEllipse`'s infill mostly straight `G1`s even on its more-curved
     regions). The largest valid prefix is found via exponential ("galloping")
     search — try 2, 4, 8, ... points, doubling — followed by a binary search
     between the last successful size and the first failed one, rather than a
     single binary search across the *full* remaining range every step: when most
     accepted segments end up short (common in practice), a plain binary search's
     first probe against the full remaining range is wasted work on every outer
     step, making the whole pass `O(n²)` (measured: infill generation on
     `horse.svg` went from 0.88s to 13.27s with a naive full-range binary search).
     Galloping keeps each step's cost proportional to the segment size actually
     found, restoring `O(n)`-amortized behavior (0.88s → 1.69s, the remainder
     being genuinely more/better-placed arcs, not overhead). This also means
     `_fitPointsToTolerance`/`_loopToPath` no longer take a `maxDepth` — each outer
     step is guaranteed to consume at least one new point (a 2-point range always
     fits), so termination doesn't need a recursion-depth safety net the way the
     continuous fitter's `maxDepth` does.
   - Gated by `isFillable()` (`lib/geometry.py`), not `isClosed()` — see the
     `Path.isFillable()` entry above.
   - **Known non-bug**: `horse.svg`'s front legs show short alternating `Line`/`Arc`
     runs in their infill even though the legs look straight. Confirmed (by printing
     signed perpendicular deviation along one such run) that this is genuine,
     sub-visual curvature in the hand-drawn source path — an S-shaped wobble up to
     ~0.45mm that a single circular arc can't represent (curvature changes sign),
     correctly preserved within the tight `0.012mm` `tessellationTolerance`. Not
     something infill-specific code can or should "fix" — changing it means loosening
     `tessellationTolerance` project-wide, which the user explicitly declined.

4. **Path ordering / routing** (`orderPaths`/`_orderSequence`, [`lib/route.py`](lib/route.py),
   called between `generateInfill` and `plotter.createFile`)
   - `_orderSequence(items, startPos, endPos)` is the actual routing algorithm,
     generalized to work on *any* list of items exposing `start()`/`end()`/
     `isClosed()`/`vertices()`/`rotateTo()`/`reverse()` — both `Path` and
     `PathObject` qualify. Reorders (and returns) `items` to approximately minimize
     travel distance. This is TSP-like but not quite TSP: each item can be drawn
     forwards or backwards for free (`reverse()`), and a *closed* item can
     additionally be entered/exited at any of its vertices (`rotateTo()`), since it
     ends where it starts.
   - Solved with nearest-neighbor construction, then three interleaved local-search
     moves run to convergence: 2-opt (try reversing runs of the tour — reversing a
     run also flips the draw direction of every open item in it, which leaves the
     connections *within* the run unchanged, so only the two boundary edges need
     comparing); anchor optimization (for each closed item, try every vertex against
     its current tour neighbors, keep the cheapest); and the *rendezvous move* (see
     below).
   - The **rendezvous move** exists because anchor optimization is coordinate descent
     — it moves one anchor at a time against its *current* neighbors, so it can't
     relocate a *group* of closed loops that should all be cut near a shared point
     (concentric infill loops sharing a seam, or the near-coincident inner/outer
     contours of a traced outline like `horse.svg`'s). Escaping that needs several
     anchors to move at once. The move snaps every closed item's anchor to a common
     rendezvous point — trying each vertex of the *smallest* closed item as the
     candidate rendezvous (the loop that most tightly constrains where the group can
     meet) — and keeps it only if total tour length drops, so it never hurts the
     spread-out case (there it's simply rejected). Without it, a chain of closed
     loops routes to a coordinate-descent local minimum that can be many× longer than
     optimal (measured ~9× on `horse.svg`'s 3 subpaths before this move was added).
   - `startPos`/`endPos` anchor the very first/last item in the tour to a fixed
     point (e.g. the plotter's physical home/park position); either may be `None`
     instead, meaning that end of the tour is free — the boundary cost term for a
     `None` side just drops to zero (in the shared `tourCost` helper and every move),
     so nearest-neighbor seeds from the first item's own start and the refinement
     moves stop comparing against it.
   - `orderPaths(document, startPos, endPos)` calls `_orderSequence` **twice, in two
     independent passes**, so cost stays bounded even when an object has many
     subpaths (e.g. dense infill or a dotted line — routing every subpath across
     every object jointly in one pass would be much slower):
     1. *Pass 1:* for each `PathObject` with more than one subpath, order its own
        `list[Path]` independently, free-start/free-end (`None`/`None`) — neither
        neighboring object is decided yet, so there's nothing to anchor to.
     2. *Pass 2:* order `document.objects` itself, anchored to the machine's
        `startPos`/`endPos`, using each object's start/end as already fixed by pass
        1 (a multi-subpath object's internal arrangement is *not* revisited here).
   - Fast enough for the stated scale (200-300 objects, well under a second) without
     needing an external TSP/routing library.

5. **Gcode generation** (`Plotter` class, [`lib/plot.py`](lib/plot.py))
   - `addPath` loops over each subpath in `object.geometry`, calling
     `path.tessellate(tessellationTolerance, maxTessellationDepth)` to reduce it to
     only `Line`/circular-`Arc` segments (adaptive — see "Adaptive tessellation"
     below), then walks those: lines become `G1` moves, arcs become native `G2`/`G3`
     arcs. No other segment types can reach this point. The travel-move-to-start
     that already precedes every `Line`/`Arc` naturally becomes the pen-lift between
     subpaths — it's a no-op when consecutive segments are already connected,
     exactly as it is within a single subpath.
   - `penMove` decides travel vs. draw moves, and whether a travel move is "short"
     (stays down, `shortTravelThreshold`) or "long" (lifts pen to travel height,
     moves, lowers again).
   - `addLine` builds one gcode line, skipping redundant params (same speed/accel/
     position as last time), and can annotate lines with `; FEATURE: X` comments so
     Bambu Studio's preview colors moves by "line type" / "instruction" / "segment"
     (purely cosmetic — see `visualization.style` in settings).
   - `createFile` passes `raised=(objectCount % 2 == 0 and objectHeightChange)` to
     `addPath` for every other object; `penMove` adds a fixed +0.001mm to the Z height
     of every travel/draw move for that object. The offset is small enough to have no
     effect on the physical print, but it's enough for Bambu Studio's slicer to treat
     alternating objects as separate Z layers in its preview — otherwise every object
     shares the same draw height and the slicer's layer view can't distinguish them
     (purely cosmetic, like `; FEATURE:` comments — see `visualization.objectHeightChange`).
   - `createFile` stitches together: `Append/startCode.gcode` (prefix) → generated
     moves for every object in the `Document` → `Append/endCode.gcode` (suffix).
     Prefix/suffix files use `{PLACEHOLDER}` tokens (e.g. `{TRAVEL_HEIGHT}`,
     `{LOAD_DELAY}`, `{BED_EXCLUDE_AREA}`) substituted from `PlotSettings` via
     `fileAppend`.

### Adaptive tessellation (`Path.tessellate`, used by `addPath` and infill)

Reduces a whole *subpath* (not one segment at a time) to only `Line`s and circular
`Arc`s (gcode's native `G1`/`G2`/`G3` primitives, or infill's required pure-`Line`
polygons when `allowArcs=False`), fit to within `tessellationTolerance` mm of the
original curve. `Path.tessellate(tolerance, maxDepth, allowArcs=True)` is non-mutating.
`maxTessellationDepth` is accepted for signature stability with existing callers but is
unused by this fitter (see "no depth cap" below).

- **Per-path, not per-segment**: an earlier version tessellated each `Segment`
  independently (`Segment.tessellate`/`_fitToTolerance`, since removed), which meant a
  segment boundary was always a hard fit boundary — consecutive collinear `Line`s never
  merged, and a run of short `Line`s tracing a circular arc stayed many `G1`s instead of
  becoming one `G2`/`G3`. The current fitter works in `Path.point(t)`'s single
  normalized `0 <= t <= 1` space spanning the whole subpath (internally rescaled to
  segment index + local `t`), so a fit can span *across* original segment boundaries.
- **Bidirectional greedy, not midpoint-split**: for a failed range, blindly splitting
  at the midpoint can break up an otherwise-straight run just because a corner happens
  to fall near the middle — e.g. `/\______` (each char one segment) should become
  `/`, `\`, `______`, not `/\`, `__`, `____`. `Path._fitRange(t0, t1, tolerance,
  allowArcs)` instead greedily grows a fit from **each end** via `Path._greedyExtent`
  and meets in the middle:
  1. `tf, front = _greedyExtent(t0, t1, ...)` — farthest a single Line/Arc can reach
     growing forward from `t0`. If it reaches `t1`, the whole range fits as one segment.
  2. `tb, back = _greedyExtent(t1, t0, ..., backward=True)` — farthest growing
     backward from `t1`.
  3. If the two overlap (`tf >= tb`), `Path._trimSegmentStart` reuses `back`'s own
     already-validated geometry (just restricting its parametric start to `tf`) rather
     than recomputing a fresh fit — fitting is *not* monotonic under range-narrowing (a
     fresh 3-point circumcircle through different points isn't guaranteed to still
     satisfy tolerance even though its range is a subset of one that already did;
     confirmed by a real crash before this fix existed).
  4. Otherwise, recurse on the unfit middle `[tf, tb]` and return `[front, ...middle,
     back]`.
  A minimal range always fits a `Line` (deviation → 0 as the range shrinks), so `tf`
  always makes forward progress and this always terminates — **no `maxDepth` cap
  needed**, mirroring the same reasoning behind infill's galloping-search rewrite.
- **`Path._greedyExtent`**: finds the farthest extent via exponential ("galloping")
  growth then a binary search between the last success and first failure — the same
  technique as infill's `_fitPointsToTolerance` (O(n)-amortized, avoids the O(n²) trap
  of binary-searching the full remaining range every step). The returned extent is only
  approximate (stops once within ~2% of a segment's length rather than fully
  converging) — a slightly-short extent just shifts a little more work onto the
  neighboring fit, never risks exceeding tolerance itself.
- **`Path._tryFitRange(t0, t1, tolerance, allowArcs)`** is the actual fit test: builds
  `_SAMPLES_PER_SEGMENT` deviation-check samples *per original segment touched by the
  range*, not spread evenly across the whole range — a large range dominated by one
  nearly-straight segment can otherwise starve a small curved fragment of samples
  (confirmed: missed a real 0.039mm spike, over 3x tolerance, in a sheared ellipse's
  arc tail). Also explicitly samples every original segment **boundary** strictly
  inside the range (the only place a multi-segment range can hide a genuine corner —
  position-continuous, direction-discontinuous — that per-segment interior sampling
  alone would never land exactly on) and a few geometrically-spaced points right next
  to `t0`/`t1` themselves (since those usually aren't original path points but wherever
  a *neighboring* range's own approximate search happened to stop, a sharp feature can
  sit just past one with no regular sample close enough to catch it). Tries `Line`
  first (true distance to the finite chord *segment*, clamped — not just perpendicular
  deviation from the infinite line, which would miss a curve that travels out along the
  chord's own direction and doubles back past an endpoint, a real "out and back" shape
  seen in infill loops), then, if `allowArcs`, a circular `Arc` via
  `Arc.fromThreePoints(point(t0), point(mid), point(t1))` (true distance to the finite
  swept *arc*, angle-clamped via `Arc._thetaToT` — same "out and back" risk, radially).
  **`MAX_RADIUS_TO_CHORD`**: `Arc.fromThreePoints`' circumcircle math is numerically
  unstable for near-collinear inputs (confirmed: an equivalent range, differing only by
  sub-ULP float noise, produced a 359mm-radius circle one call and a 581mm-radius circle
  the next) — a well-fit arc's chord is always a meaningful fraction of its radius, so a
  radius/chord ratio this extreme is itself the signal that the fit is numerical noise,
  not real gentle curvature; rejected outright rather than trusted.
- **Already-circular `Arc` segments are kept atomic** (arcs mode only): a segment where
  `abs(abs(u)-abs(v)) <= tolerance` is emitted unchanged as a hard fit boundary, rather
  than folded into a surrounding run. This keeps exact circles exact and sidesteps a
  real degeneracy — a full-circle `Arc` has `start == end`, so `Arc.fromThreePoints`
  would see `p0 == p1` and (correctly) refuse to fit a circumcircle, which would
  otherwise force the whole circle down to tiny `Line` fallbacks. When `allowArcs=False`
  (infill needs pure-`Line` polygons) there's no atomic handling — every segment, arcs
  included, is sampled via `point(t)` and line-fit, so a `<circle>` still flattens to a
  valid polygon.

## Settings (`settings.json`, loaded via `commentjson` so `//` comments are allowed)

Loaded into `PlotSettings` ([`lib/plot.py`](lib/plot.py)) by `initFromJson`, which
validates setting names against the dataclass fields (`{f.name for f in fields(self)}`
— pass the *instance*, not the class name, or Pylance's `fields()` overload can fail to
recognize the not-yet-fully-processed class as satisfying `DataclassInstance` when
referenced by name from inside one of its own methods) and prints a warning for
anything unknown (no full type-checking yet — see `#TODO`).

- `machine`: `startPos` (nozzle X/Y/Z home), `penOffset` (pen vs. nozzle, since the pen
  is mounted offset from where the nozzle would be), `plateSize` (256x256 for P1S),
  `drawableArea` (safe region the pen can reach on the paper).
- `gcode`: per-`State` (`draw`/`travel`, plus debug bounding-box states) heights/
  speeds/accels, `shortTravelThreshold`, `tessellationTolerance`/
  `maxTessellationDepth` (see "Adaptive tessellation" above), `infillSpacing` (mm
  between concentric infill loops — see "Infill generation" above; `<= 0` disables
  infill; default `0.3` is slightly under `penWidth` `0.35` so adjacent strokes
  overlap slightly rather than leaving gaps), and the prefix/suffix gcode file paths.
- `visualization`: cosmetic-only settings controlling how the gcode looks in Bambu
  Studio's preview (pen width for line rendering, `lineTypes` per state, `loadDelay`
  seconds to wait for the user to load the pen, `showPenPos` toggle,
  `objectHeightChange` toggle for alternating +0.001mm Z per object so Bambu Studio's
  preview shows each object as a separate layer, and the `style`/`styleLineOrder`
  coloring scheme described in the comments in `Plotter.addLine`, `lib/plot.py`).
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
  clamps to `[-5000, 5256]` (`lib/geometry.py`).
- `Append/startCode.gcode` / `endCode.gcode` are real Bambu Studio start/end gcode
  (home, bed leveling skip, fan/temp no-ops, motor current tweaks) with placeholders
  swapped in; they were renamed from `startcode`/`endcode` because `commentjson`
  choked on special characters when they were inline in the JSON.
- `; FEATURE: X` comments are how Bambu Studio's slicer preview assigns per-move
  colors — hijacked here purely for visualizing plot order/type, not for anything
  print-quality related.

## Known TODOs / rough edges (from comments in the code)

- `fileIn`/`fileOut` should eventually be user-prompted instead of hardcoded.
- Color (`strokeColor`/`fillColor` hex→RGB) isn't implemented; only *presence* of
  fill (`None` vs. not) is used, for infill gating — the actual RGB values in
  `Style` are still unused placeholders.
- `Document.add()` has a noted bug: adding an object whose `id` collides with an
  existing one silently overwrites the `id` lookup without warning.
- The print-area scale line in `parseSvg` (`#transform.scale(dimensions.imag / svg.height)`)
  is still commented out — nothing currently scales the drawing to fit `drawableArea`;
  the SVG is plotted at its authored physical size (mm).
- No type-checking of values loaded from `settings.json` yet.

## Dependencies

Beyond the standard scientific-Python stack (`scipy`, used for `Segment.length()` via
`quad`), `svgelements` (SVG parsing) and `commentjson` (JSONC settings): **`pyclipper`**
(`pip install pyclipper`), for infill's polygon offsetting — see "Infill generation"
above. `shapely` is also installed but not used (can't cleanly do `nonzero` fill).

`pyclipper` is a compiled Cython extension and ships no type stubs of its own (no
`types-pyclipper`/`pyclipper-stubs` package exists either), so Pylance/Pyright can't
resolve any `pyclipper.X` call without one. [`typings/pyclipper/__init__.pyi`](typings/pyclipper/__init__.pyi)
is a hand-cleaned stub generated by pointing `stubgen` at the *compiled submodule*
directly (`stubgen -m pyclipper._pyclipper`, which introspects the installed `.pyd` at
runtime — `stubgen -m pyclipper` alone just re-emits the package's own `from
._pyclipper import *` literally and produces a useless stub). Pylance/Pyright pick up
`./typings` automatically (the default `stubPath`), no config needed.

## Files

- `_Process.py` — entry point: hardcoded `fileIn`/`fileOut`, then the run sequence
  (`parseSvg` → `generateInfill` → `orderPaths` → `plotter.createFile`). No pipeline
  logic of its own.
- `lib/` — the actual pipeline, split so each module only depends on `geometry` (a DAG,
  no cycles) and so the pipeline stages are independently testable via plain imports:
  - `lib/geometry.py` — `Style`, `Transform`, `Segment`+subclasses (`Line`, `Arc`,
    `QuadraticBezier`, `CubicBezier`), `Path`, `PathObject`, `Document`. Depends on
    nothing else in `lib/`.
  - `lib/plot.py` — `State`, `PlotSettings`, `Plotter` (gcode generation/I/O). `State`
    lives here rather than in `geometry.py` because it's used exclusively for plotting
    (heights/speeds/accels/lineTypes keys, `; FEATURE:` selection) — nothing about it is
    a geometry concern.
  - `lib/svgparse.py` — `parseSvg`/`parseSvgElement`/`readStyle`: SVG → `Document`.
  - `lib/route.py` — `orderPaths`/`_orderSequence`: the routing/ordering algorithm.
  - `lib/infill.py` — `generateInfill`: concentric infill generation (needs
    `pyclipper`; degrades to a no-op with a warning if it's not installed).
- `settings.json` — machine/gcode/visualization/debug config, loaded by
  `PlotSettings.initFromJson` (`lib/plot.py`).
- `typings/pyclipper/__init__.pyi` — hand-cleaned type stub for `pyclipper` (see
  "Dependencies" above), since it ships none of its own.
- `Append/startCode.gcode`, `Append/endCode.gcode` — prefix/suffix gcode templates.
- `*.svg` (`horse.svg`, `horseSmall.svg`, `test2.svg`) — sample input drawings.
  `testDrawing.svg`'s shapes are individually named for what they test (fill vs.
  `fill:none`, `nonzero` vs. `evenodd`, multi-region self-intersecting fills,
  transformed fills, filled-but-open paths, etc.) — see "Infill generation" above;
  `horse.svg` additionally exercises a real multi-subpath compound `<path>`.
- `*.gcode` (`horse.gcode`, `testDrawing.gcode`) — generated output, not source of truth.
