G17 ; use XY plane for arcs
G21 ; use mm
G90 ; absolute pos
M221 S0 ; disable e-axis

M104 S0 ; disable extruder heating
M140 S0 ; disable plate heating
G28 ; home

M204 S{TRAVEL_ACCEL}
G1 Z{TRAVEL_HEIGHT + 15} F{TRAVEL_SPEED}; move to safe pos
G1 X75 Y75
; allow pen to be loaded
G4 S{LOAD_DELAY}

; LAYER_HEIGHT: 0.2
; LINE_WIDTH: {LINE_WIDTH}
