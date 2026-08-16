<!--
This file is intended for non-technical people, and should not discuss technical details
Do not write things the user can see for themself.
For example, do not write that the calibration tests have every 10th line longer. The user will be able to see that once the test is plotted.
-->
# Pen Plotter

## Calibration

Every pen, printer, and sheet of paper behaves a little differently - how hard the
pen presses, how fast it can move before lines fade, how close two lines can be
before they blur into one. Rather than guessing at these and printing full drawings
until one looks right, this program can print **calibration sheets**: test patterns
that lay out a range of values side by side, so you can look at the paper and read
off the one that works best.

### Running a calibration test

1. Open the file `config/machines/bambu_p1s.json` in any text editor.
2. Find the line that says `"calibrationTest": "none"`.
3. Change `none` to the name of the test you want to run (see below), keeping the
   quotes, for example `"calibrationTest": "height"`.
4. Save the file, then run `_Process.py` as usual. Instead of asking for a drawing,
   it will ask you a few questions specific to that test, then create a file named
   after the test (e.g. `test_height.gcode`).
5. Open that file in Bambu Studio and print it just like a normal drawing.
6. When you're done, change `calibrationTest` back to `"none"` so the program goes
   back to drawing your SVG files normally.

### Available tests

Run these in order - each one finds a setting the tests after it depend on.

#### `height`

Nothing else can be calibrated until the pen reliably touches the paper. Asks for a
minimum, maximum, and step size in mm. Prints one short horizontal line per height,
stacked with the lowest height at the bottom.

Larger heights press the pen less firmly against the paper, so the lines should fade
out as you look up the stack. The **highest height that still draws a solid, dark
line** is the value to put in `motion.heights.draw` (or `.stroke`/`.infill`/etc. if you
want to set them individually). The **lowest height that leaves no mark on the paper at
all** is a floor for `motion.heights.travel` - travel should sit at or above that so
the pen never drags while moving between shapes.

#### `speed`

Asks for a minimum, maximum, and step size in mm/s. Prints one line per speed, laid
out the same way as the `height` sheet but each row is 50mm long - long enough that the pen is moving at
full speed for most of the line rather than still accelerating out of the corner.

The **fastest line that is still solid and dark all the way along** is the value to
put in `motion.speeds.draw`. A line that goes faint, or fades out, partway along was drawn faster than the pen could keep up with.

#### `accel`

Asks for a minimum, maximum, and step size in mm/s². A straight line can't reveal an
acceleration problem - nothing on it ever changes direction - so each row is a zigzag
instead: a few wide zigzags followed by several narrow ones.

Too much acceleration makes lines wobbly. The **highest acceleration where every corner
in both the wide and the narrow zigzag still comes to a clean point**, with no rounding
or skipping, is the value to put in `motion.accels.draw`.

*(More tests are added here as they're built.)*
