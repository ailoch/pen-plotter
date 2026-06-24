
M204 S7500
G17

; FEATURE: Custom
; MACHINE_END_GCODE_START
; filament end gcode 

M400 ; wait for buffer to clear

MOVE_TRAVEL_HEIGHT
G1 X128 Y256 F9000
G1 Z50 F600
G1 Z48

M400 P100

M220 S100  ; Reset feedrate magnitude
M201.2 K1.0 ; Reset acc magnitude
M221 S100 ; Reset e-axis magnitude
M73.2   R1.0 ;Reset left time magnitude
M1002 set_gcode_claim_speed_level : 0

M17 X0.8 Y0.8 Z0.5 ; lower motor current to 45% power
; EXECUTABLE_BLOCK_END