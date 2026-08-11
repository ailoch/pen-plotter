"""End-to-end smoke tests: run the whole pipeline over every fixture SVG.

These assert only that the pipeline *runs* and produces plausible gcode - not
what the gcode contains. This is deliberate - changes in the settings would
cause different gcode output, which would cause these tests to fail if we checked gcode
What these catch is the large class of regressions that show up as an
exception, a silently empty document, or a write failure.
"""
import os
import glob
import pytest


from lib.svgparse import loadSvg, parseSvg, SvgParseError
from lib.stroke import generateStroke, dropRawGeometry
from lib.infill import generateInfill
from lib.route import orderPaths
from lib.plot import createFile

#region helpers

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# fixtures that are supposed to be rejected by loadSvg
BAD_SVGS = {"invalid.svg", "viewport-negative-size.svg"}


def _drawingSvgs() -> list[str]:
    """Every fixture SVG that should parse, as repo-relative paths."""
    found = sorted(glob.glob(os.path.join(TESTS_DIR, "*.svg")))
    found = [p for p in found if os.path.basename(p) not in BAD_SVGS]
    # Top-level drawings exercise real-world geometry the fixtures don't.
    # horse.svg isn't in the repo (uncertain license) so it's included only when
    # present, rather than being a hard requirement for anyone cloning this.
    for extra in ("horse.svg", "testDrawing.svg"):
        path = os.path.join(os.path.dirname(TESTS_DIR), extra)
        if os.path.isfile(path):
            found.append(path)
    return found


def _fitScale(svg, settings) -> tuple[float, float]:
    """The scale promptRescale's fit-to-canvas option would choose."""
    scale = min(
        settings.canvasSize.real / float(svg.viewbox.width),
        settings.canvasSize.imag / float(svg.viewbox.height),
        1.0,
    )
    return scale, scale


def runPipeline(svgPath: str, settings):
    """parse -> stroke -> infill -> drop raw -> route. Returns the Document."""
    svg = loadSvg(svgPath)
    document = parseSvg(svg, settings, *_fitScale(svg, settings))
    generateStroke(document, settings)
    generateInfill(document, settings)
    dropRawGeometry(document)
    orderPaths(document, settings)
    return document


#endregion

#region pipeline


@pytest.mark.parametrize("svgPath", _drawingSvgs(), ids=os.path.basename)
def testPipelineRuns(svgPath, settings, tmp_path):
    """The full pipeline completes and writes non-trivial gcode."""
    document = runPipeline(svgPath, settings)

    # every fixture draws *something*; an empty document means a parse or
    # generation stage silently dropped everything
    assert document.objects, "pipeline produced no objects"
    assert any(obj.geometry for obj in document.objects), "no drawable subpaths"

    out = tmp_path / "out.gcode"
    assert createFile(document, settings, str(out)), "createFile reported failure"
    assert out.exists(), "createFile returned success but wrote no file"
    assert out.stat().st_size > 0, "output file is empty"

    text = out.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    assert lines, "output file has no lines"

    # A file containing nothing but the prefix and suffix templates would still
    # be large and would still "succeed" - the P1S templates alone are ~152
    # lines. So the bar has to be the templates' own size, not a fixed number,
    # or an empty drawing sails past it.
    templateLines = 0
    for path in (settings.prefixFile, settings.suffixFile):
        with open(path, encoding="utf-8") as f:
            templateLines += sum(1 for _ in f)
    assert len(lines) > templateLines, (
        f"output is {len(lines)} lines but the prefix+suffix templates alone are "
        f"{templateLines}, nothing was drawn between them"
    )

    # Draw moves carry E, travel moves don't (lib/plot.py's _penMove), so this
    # counts actual pen-down drawing rather than repositioning. G2/G3 count too:
    # tessellation emits arcs as G2/G3, so a shape made only of arcs (any circle)
    # produces no E-bearing G1 at all and would look undrawn if we only counted G1.
    drawMoves = sum(
        1 for ln in lines
        if ln.startswith(("G1 ", "G2 ", "G3 ")) and " E" in ln
    )
    assert drawMoves > 0, "gcode contains no pen-down draw moves (E-bearing G1/G2/G3)"


@pytest.mark.parametrize("name", sorted(BAD_SVGS))
def testInvalidSvgsRejected(name, settings):
    """Malformed / unusable SVGs raise SvgParseError rather than crashing later."""
    with pytest.raises(SvgParseError):
        loadSvg(os.path.join(TESTS_DIR, name))


def testRawGeometryFullyDropped(settings):
    """No RAW_GEOMETRY survives into routing - it has no height/speed entry, so
    anything left would blow up (or silently mis-route) in plot.py."""
    from lib.settings import LineType
    document = runPipeline(os.path.join(TESTS_DIR, "comprehensive.svg"), settings)
    leftover = [
        (obj.id, p.lineType)
        for obj in document.objects
        for p in obj.geometry
        if p.lineType == LineType.RAW_GEOMETRY
    ]
    assert not leftover, f"RAW_GEOMETRY survived dropRawGeometry: {leftover[:5]}"


def testEveryDrawnRoleHasMotionSettings(settings):
    """Every lineType that reaches routing must have heights/speeds/accels
    entries - a missing one is a KeyError deep inside gcode generation."""
    document = runPipeline(os.path.join(TESTS_DIR, "comprehensive.svg"), settings)
    roles = {p.lineType for obj in document.objects for p in obj.geometry}
    for role in roles:
        assert role in settings.heights, f"{role} missing from motion.heights"
        assert role in settings.speeds, f"{role} missing from motion.speeds"
        assert role in settings.accels, f"{role} missing from motion.accels"


#endregion
