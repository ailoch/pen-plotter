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

*(More tests are added here as they're built.)*
