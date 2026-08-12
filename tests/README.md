# Tests

Automated tests (pytest) plus the SVG fixtures they and manual inspection run
against. Every fixture element carries an `id` naming **what it tests**; the
parser prints that `id` when it drops an unsupported element (`Warning: ignored
unsupported element type <type> named '<id>'.`) or warns about text, so a
dropped case is self-identifying in the console.

## Running

```sh
py -3 -m pytest              # everything, ~40s
py -3 -m pytest -m "not slow"  # smoke only, ~20s
```

`test_geometry.py` and `test_plot.py` need no pyclipper and run in ~2s each —
the quickest check that a change to the geometry primitives or the gcode
emitters is sound.

`git config core.hooksPath .githooks` enables [`.githooks/pre-push`](../.githooks/pre-push),
which runs the suite before any push (commits stay unhooked; `git push
--no-verify` bypasses).

Tests come in two shapes. **Cross-cutting** suites run the real pipeline and
assert a property that spans modules; **per-module** suites (`test_<module>.py`)
build geometry directly and assert behaviour with a specific right answer.

| File | Scope | Covers |
|------|-------|--------|
| `test_smoke.py` | cross-cutting | Full pipeline over every fixture + `testDrawing.svg`: completes without exception, emits non-trivial gcode, rejects the two deliberately-invalid SVGs, leaves no `RAW_GEOMETRY` alive past `dropRawGeometry`, and confirms every surviving `LineType` has `heights`/`speeds`/`accels` entries. Asserts nothing about gcode *content* — speeds, spacing and routing all legitimately change it |
| `test_coverage.py` | cross-cutting | The coverage invariant: for every object, the region SVG would paint (fill region + stroke band, recomputed independently of `generateInfill`) minus every drawn centerline swept by ±`fillSpacing/2` leaves nothing thicker than `GAP_HALF_WIDTH_FRACTION` — a known-issues baseline sitting just above today's worst gap. Also asserts disabling `generateGapInfill` measurably worsens coverage, so the residue pass can't become a silent no-op. Marked `slow` |
| `test_stroke.py` | per-module | `_passDeltas`' pass-offset arithmetic (even pitch, outermost pass landing exactly on `strokeWidth/2`, both parities); `_passCount`'s near-integer snap (a width that is an exact multiple of `fillSpacing` gets one pass per multiple, not an extra one from float noise — including the parse-noise values either side of 0.3 — while a genuine non-multiple still rounds up, and the snap never coarsens the pitch past `fillSpacing`); and the dash walk: inked length equals the pattern's duty cycle (including the 6-entry pattern an odd-length `dasharray` normalizes to — the walk only ever sees an already-even pattern, so the repeat itself is `test_geometry.py`'s), `dashoffset` shifts the pattern (negative shifts backwards) without changing total ink on a closed loop, the seam dash merges across a closed path's start point, and arcs survive dashing. Pipeline-level: dashes ink less than solid and never outside the solid band, the `generateStroke=False` fallback still dashes, and a dashed stroke over a fill draws **only its outward half** — with the two guards that it stays two-sided when there's no fill and when the shape encloses no area |
| `test_plot.py` | per-module | The gcode emitters, driven against an in-memory file and asserted line by line, in the order plot.py defines them. `_fmtNum`'s zero-trimming; `_moveRect`'s debug box. The `{...}` template evaluator: arithmetic against the namespace, and that every construct outside the whitelist — attribute traversal (the escape a bare `eval` allows), calls, subscripts, `**`, string/bool literals, arithmetic on a string value — is left verbatim with a warning rather than executed. `style: "segment"`'s colour cycle, including that a closed path never ends on the colour it started with (4 segments over 3 colours give `a-b-c-b`, not `a-b-c-a`). `_addLine`'s falsy-arg elision, unchanged-axis skipping, the G2/G3 exemption from needing an axis at all, and `F`/`M204` going out only when they change. `_eValue`'s E scaling, including that a **non-positive** `eAxisMultiplier` collapses to a droppable 0 rather than a negative (truthy, so it would survive the falsy-arg elision and retract on every draw move). Pen moves: lift/move/lower on a long travel, staying down on a short one, the min-of-both-roles threshold in **both** directions (an arrival-only lookup passes the easy one), the ±0.001mm preview raise, and G2 vs G3 from the arc's sweep. `_splitAtBounds`: both bbox fast paths handing back the original object, one- and two-edge crossings, an arc's pieces joining up exactly and still spanning the whole, and a corner graze staying whole. `_addPath`'s crop-vs-mark modes and one-name-per-object reporting. The bed exclude area, asserted three ways over every canvas-vs-plate arrangement — even-odd membership on a sampled grid, signed area (which is what catches the ring hole's opposite winding, invisible to even-odd), and no redundant vertices — plus an overhanging canvas clamped back onto the plate. `_waitForPen`'s pen-load countdown: the `M73` percentages run 100→0 strictly decreasing, the split dwell still lasts exactly `loadDelay` (including a delay that doesn't divide into whole milliseconds — the case that catches per-step rounding drift), the step stays inside the 1%–5% clamp and under the 500ms tick, a long delay falls back to the 1% floor, and the toggle off / a non-positive delay each collapse to a bare `G4 P`. `createFile` end to end: every substitution reaching the file, `showPenPos`'s two coordinate spaces, and the temp-file swap leaving an existing output intact when the write fails partway |
| `test_geometry.py` | per-module | The primitives everything else is built on. `Style.dashPattern()`'s SVG normalization (odd-length repeat, all-zero → solid, and that it doesn't mutate the style). `Transform`: each operation's effect, that accumulated ops apply in the global frame, `@` vs `*` composing in opposite orders, raw-matrix coercion, and the two argument edge cases a falsy check gets wrong (an explicit `0` for `translate`/`scale`'s second argument) plus rotation about a point holding that point fixed. The `Segment` ABC contract — it can't be instantiated, and every primitive implements the whole interface (each method actually called, plus `reverse`/`applyTransform` round-trips). `Segment.toPoints`: the point-symmetric S-cubic that a midpoint-only flatness check would collapse to a straight line, and that the returned polyline really stays within tolerance. Per-primitive `point`/`derivative`/`length`/`reverse`/`subsegment`/`tAtPoint`, the `@` intersection operator across every Line/Arc pairing (including tangency collapsing to one point and elliptical arc-arc raising), and `bounds()` for all four primitives — a bezier's box follows the true curve, not its control polygon, and clamps to the renderer's `[-5000, 5256]`. `Path`: `point(t)` spanning the whole subpath, `isClosed` vs `isFillable` being independent, bowtie lobes not cancelling, `rotateTo`/`reverse`/`vertices`, and `tessellate` carrying `lineType` through non-mutatingly. `PathObject.applyTransformations` reaching every segment type's control points and scaling `strokeWidth`/`dasharray`/`dashoffset` together by `sqrt(\|det\|)`. `Document.add` ordering, the known duplicate-`id` collision, and bounds aggregating at each level |
| `test_infill.py` | per-module | Fill-rule resolution: the same wound-alike donut fills solid under `nonzero` and leaves a hole under `evenodd`. A matched pair — together they imply the rules differ, so no separate inequality test is needed. Plus the residue shape: an acute wedge's too-thin pieces come out as **open medial-axis centrelines**, not the doubled-back loops a concentric fill would leave |
| `test_fallbacks.py` | cross-cutting | What happens when an optional dependency isn't installed — paths every other suite either skips past or never reaches. Without **pyclipper**: strokes collapse to one centerline `deepcopy` per subpath (arcs and beziers surviving as themselves, the raw geometry left alone for infill, and dashes still applied — they're split before the multi-pass branch), infill generates nothing but still closes an open fillable outline in place, and each module warns exactly **once per run** rather than once per object (and not at all when `fillSpacing <= 0` already disabled fill). Without **scipy/numpy**: residue gap fill falls back from open medial-axis centerlines to closed `_fillGap` loops (the medial-axis side of that pair is `test_infill.py`'s). The two dependencies are patched out differently because they're imported differently — `pyclipper` is bound as a module attribute at import, so `monkeypatch.setattr` on that name is what a missing install looks like; scipy/numpy are imported lazily *inside* `_medialAxisLines`, so there's no attribute to overwrite and the import itself has to fail, via a `None` entry in `sys.modules` |
| `test_settings.py` | per-module | `Settings.initFromJson`: fallback to defaults on a missing/malformed/wrong-shape file, the containment warnings, `"draw"` expansion and per-role override, the mm/s→mm/min conversions, alignment resolution to a lower-left offset, and that every wrongly-typed setting is both skipped *and* reported. Config fixtures live in `configs/` |

Config fixtures for `test_settings.py` live in `configs/`: `invalid.json`
(malformed), `not-sections.json` (valid JSON, wrong shape) and
`wrong-types.json` (every setting named correctly and typed wrongly). Cases that
are about a *value* rather than the file are built inline in the test.

Status tags used in the SVG comments:

| Tag | Meaning |
|-----|---------|
| `[OK]` | parsed and drawn today |
| `[WARN]` | parsed but prints a warning (e.g. text) |
| `[DROP]` | element type not handled → silently ignored |
| `[PARTIAL]` | drawn, but some attribute is ignored (e.g. stroke dashes/markers) |

## `comprehensive.svg`

One file, `viewBox` matches the currently-configured `canvasSize` (215.9×243
mm), so it loads at scale (1,1) with no rescale prompt. Organized into
top-level `<g>` bands, each split into subgroups:

| Group | Covers |
|-------|--------|
| `basic-shapes` | rect (filled), rounded-rect (including corners) `[OK]`, circle, ellipse, `<line>`/`<polyline>`/`<polygon>` `[OK]`, `<use>` `[DROP]`, arc + rotated-ellipse arc, quadratic/cubic bezier, shorthand path commands (`h`/`v`/`t`), text-as-path (converted) `[OK]`, and a raw live `<text>` `[WARN]` |
| `transforms` | translate, non-uniform scale, rotate-about-point, skewX, skewY, raw matrix, negative-scale mirror, transform on an arc, group-inherited transform — all on one reference "F" glyph |
| `fill` | evenodd/nonzero donuts, open filled path, 2 & 3 regions, nested subpaths, degenerate single line, self-intersecting figure-eight `[OK]` (`fill-figure-eight-selfintersect` — exercises `Path.isFillable()`'s self-intersection handling, see comment in the SVG) |
| `fill-gapfill` | acute wedge, tapering slot, region below `fillSpacing`, thin sliver, concentric circle — the cases `_drawResidue` exists to handle. These now fill predominantly as open `GAP_INFILL` **centerline strokes** (the piece's medial axis), not closed loops; a genuinely wide leftover still falls back to `_fillGap` loops |
| `stroke` | varying widths (thin/medium/thick), zigzag, multiple subpaths, self-intersection, dashes `[OK]` — `stroke-dash-basic`/`-complex-pattern`/`-offset` cover pattern and offset on open lines, and `stroke-dash-filled` is a **dashed outline over a fill**: the fill tiles from the raw centerline (it does *not* inset by `strokeWidth/2` the way a solid-stroked shape does, or every dash gap would leave an uninked notch) while `lib/stroke.py` draws only the outward half of each dash, so the two abut at the centerline without doubling up. Being closed, it also exercises the seam merge where a dash wraps the start point, joins (bevel/round/miter + miterlimit, thickened to 4mm on an acute-angled V so the join shapes actually differ) `[OK]`, caps (butt/round/square) `[OK]`, markers `[PARTIAL]`; a `stroke-expansion` subgroup covers a wide multi-pass closed stroke, combined stroke+fill (fill inset following the stroke's inner edge), a non-uniformly-transformed stroke width, and a stroke="none"+fill="none" shape that's dropped entirely. Real multi-pass generation via `lib/stroke.py` — width/joins/caps/miterlimit/dashes are `[OK]`. The acute-V join cases are also what exercises stroke residue: a miterlimit-beveled spike leaves wedges between passes, which come out as open `GAP_INFILL` centerline strokes over the stroke band |
| `structure-misc` | nested groups, fill inheritance + override, `<use>`/`<symbol>` `[DROP]`, clipPath/mask/pattern `[PARTIAL]`, `display:none` & `visibility:hidden` `[OK]` (neither is drawn), fractional opacity `[PARTIAL]` (drawn at full strength - see `opacity-ignored`) vs. `opacity="0"` `[OK]` (not drawn - see `opacity-zero-not-drawn`), a 7-segment polygon (`heptagon-segment-color-wrap`) whose segment count doesn't divide evenly into `visualization.segmentTypes`' length — exercises `lib/plot.py`'s `_skipRepeatedClosingColor` fix for `style: "segment"`'s closing-color-repeat bug |
| `degenerate` | zero-length line, zero-radius circle, empty path (dropped - nothing to draw or route), coincident points |
| `canvas-bounds` | deliberately hangs off the canvas's right/bottom edges - exercises `lib/plot.py`'s `_splitAtBounds` (cropping/marking segments outside the canvas, gated on `processing.showOutOfBounds`): a rect and a circle each fully outside (bbox fast-path, one per segment type), a 2-segment polyline crossing the boundary twice (once per segment), a 3-segment polyline crossing three times, a single line crossing two *different* edges (bottom + right, clipping through the corner region between two outside endpoints), a single line crossing two *opposite* edges (top + bottom, spanning the full canvas height), a circle straddling one edge (2 crossings - a single connected excursion), a circle bulging through two edges without reaching the corner point itself (4 crossings, two separate excursions - the multi-crossing case on a single `Arc`) |

### Text
`text-object-as-path` is `<text>` converted via Inkscape's **Path → Object to
Path** — outlines the parser draws like any other path `[OK]`.
`text-raw` is a second, deliberately **unconverted** live `<text>`
element — it exercises the parser's rejection path (the "does not support text"
warning, naming this id, and the element being omitted from gcode). Don't
convert this one.

## Viewport fixtures

Minimal SVGs, each just a border rect spanning the viewBox edges plus an
asymmetric corner marker, to eyeball scaling / centering / Y-flip against the
plate. They drive `promptRescale`'s branches:

All sized off `testDrawing.svg` (215.89999×230 mm), not the currently-configured
`canvasSize` (217×243) — same rationale as `comprehensive.svg` above, so these
currently trigger a rescale prompt too until `canvasSize` is updated to match.

| File | viewBox vs `testDrawing.svg` size | Expected prompt (once canvasSize matches) |
|------|-------------------|-----------------|
| `viewport-exact.svg` | identical (215.89999×230) | none — scale (1,1) |
| `viewport-aspect-match.svg` | 2× size, same aspect (431.79998×460) | collapsed: keep (`k`) vs rescale-to-fit (`b`) |
| `viewport-aspect-mismatch.svg` | different aspect (width matches, height 100 arbitrary) | full: keep / fit-width / fit-height / stretch |
| `viewport-nonzero-origin.svg` | same size, origin (−30,−20) | none; tests viewBox-origin handling |
| `viewport-negative-size.svg` | negative width/height (`0 0 -215.89999 -230`) | `SvgParseError` — rejected like a missing viewBox |

The corner marker sits at the SVG-min corner (min-x, min-y), which after the
pipeline's Y-flip lands at the **top-left of the plate** — use it to confirm the
drawing isn't mirrored or shifted.

## `invalid.svg`

Malformed XML (an unclosed string mid-attribute) — exercises `loadSvg`'s
`SvgParseError` path:

```sh
py -3 -c "from lib.svgparse import loadSvg; loadSvg('tests/invalid.svg')"
```
