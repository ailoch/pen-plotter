M201 X20000 Y20000 Z500 E5000 ; hard accel limits
M203 X500 Y500 Z20 E30 ; hard speed limits
M204 P20000 R5000 T20000 ; initial accel
M205 X9 Y9 Z3 E2.50 ; max jerk

M710 A1 S255 ;turn on MC fan
M104 S0

;===== reset machine status =================
M290 X40 Y40 Z2.6666666 ; allow plate to move by setting current pos (I think - not fully sure what this does)
G91 ; rel pos
M17 Z0.4 ; lower the z-motor current
G380 S2 Z30 F300 ; safe plate move
G380 S2 Z-25 F300
G90 ; abs pos
M17 X1.2 Y1.2 Z0.75 ; reset motor current to default
M220 S100 ; reset feedrate
M221 S10 ; reduce e-axis magnitude (10% is the P1S firmware's minimum accepted value; below it the command is silently discarded)
M73.2 R1 ; reset time left magnitude
M1002 set_gcode_claim_speed_level : 5
G29.1 Z0 ; clear z-trim value
G29.2 S0; disable ABL
M204 S{TRAVEL_ACCEL}
M975 S1 ; enable vc

G28 ; home
G1 F{TRAVEL_SPEED}

M83 ; rel extrusion
M400
G21 ; use mm

; allow pen to be loaded
{WAIT_FOR_PEN}
