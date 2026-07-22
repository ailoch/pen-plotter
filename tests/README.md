# Test drawings

Fixtures for exercising the SVG → gcode pipeline. Every element carries an `id`
naming **what it tests**; the parser prints that `id` when it drops an
unsupported element (`Ignored <type> with name <id>`) or warns about text, so a
dropped case is self-identifying in the console.

Status tags used in the SVG comments:

| Tag | Meaning |
|-----|---------|
| `[OK]` | parsed and drawn today |
| `[WARN]` | parsed but prints a warning (e.g. text) |
| `[DROP]` | element type not handled → silently ignored |
| `[PARTIAL]` | drawn, but some attribute is ignored (e.g. stroke dashes/markers) |

## `comprehensive.svg`

One file, `viewBox` matches `testDrawing.svg`'s size (215.89999×230 mm) rather
than the currently-configured `canvasSize` (217×243) — `canvasSize` is expected
to be updated to match `testDrawing.svg` at some point, at which point this
also stops triggering a rescale prompt. Organized into top-level `<g>` bands,
each split into subgroups:

| Group | Covers |
|-------|--------|
| `basic-shapes` | rect (filled), rounded-rect (including corners) `[OK]`, circle, ellipse, `<line>`/`<polyline>`/`<polygon>` `[OK]`, `<use>` `[DROP]`, arc + rotated-ellipse arc, quadratic/cubic bezier, shorthand path commands (`h`/`v`/`t`), text-as-path (converted) `[OK]`, and a raw live `<text>` `[WARN]` |
| `transforms` | translate, non-uniform scale, rotate-about-point, skewX, skewY, raw matrix, negative-scale mirror, transform on an arc, group-inherited transform — all on one reference "F" glyph |
| `fill` | evenodd/nonzero donuts, open filled path, 2 & 3 regions, nested subpaths, degenerate single line, self-intersecting figure-eight ⚠️ *now draws nothing at all — see Known gaps* |
| `fill-gapfill` | acute wedge, tapering slot, region below `fillSpacing`, thin sliver, concentric circle — the cases `_gapFill` exists to handle |
| `stroke` | varying widths (thin/medium/thick), zigzag, multiple subpaths, self-intersection, dashes (pattern + offset) `[PARTIAL]`, joins (bevel/round/miter + miterlimit, thickened to 4mm on an acute-angled V so the join shapes actually differ) `[OK]`, caps (butt/round/square) `[OK]`, markers `[PARTIAL]`; a `stroke-expansion` subgroup covers a wide multi-pass closed stroke, combined stroke+fill (fill inset following the stroke's inner edge), a non-uniformly-transformed stroke width, and a stroke="none"+fill="none" shape that's dropped entirely. Real multi-pass generation via `lib/stroke.py` — width/joins/caps/miterlimit are `[OK]` |
| `structure-misc` | nested groups, fill inheritance + override, `<use>`/`<symbol>` `[DROP]`, clipPath/mask/pattern `[PARTIAL]`, `display:none` & `visibility:hidden` ⚠️ *drawn anyway — see Known gaps*, opacity `[PARTIAL]` |
| `degenerate` | zero-length line, zero-radius circle, empty path (dropped - nothing to draw or route), coincident points, off-canvas rect |

### Text
`text-object-as-path` is `<text>` converted via Inkscape's **Path → Object to
Path** — outlines the parser draws like any other path `[OK]`.
`text-raw` is a second, deliberately **unconverted** live `<text>`
element — it exercises the parser's rejection path (the "does not support text"
warning, naming this id, and the element being omitted from gcode). Don't
convert this one.

### Known gaps surfaced by these fixtures
Not fixed here — each documented in its own SVG comment, to be picked up as
separate follow-up commits:

- **`display:none`/`visibility:hidden` are drawn anyway.** Neither attribute is
  checked anywhere in `lib/svgparse.py` — this is a real gap in the converter,
  not an SVG spec issue or an Inkscape bug (a real browser correctly resolves
  `visibility:hidden` to a non-painted, hidden element; Inkscape's own canvas
  view has a known history of not honoring `visibility` the same way it honors
  `display:none`, but that's an editor-display quirk separate from what our
  parser does with the parsed attribute).
- **`fill-figure-eight-selfintersect` now draws nothing.** `Path.isFillable()`'s
  plain shoelace area calculation is fill-rule-blind, so a self-intersecting
  bowtie's two opposite-wound lobes cancel to ~zero net signed area and it's
  judged unfillable — even though pyclipper's evenodd union (fill-rule-aware)
  would resolve it into two real triangles if ever reached. Pre-dates stroke
  generation; previously masked because every `RAW_GEOMETRY` subpath always got
  an outline pass regardless of fillability, so *something* was visible even
  though it was never actually filled.
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
