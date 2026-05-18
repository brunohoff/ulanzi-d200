"""
Hardware constants for the Ulanzi D200 stream deck.
"""

import struct
from typing import Dict, List

# Physical button layout of the Ulanzi D200 (landscape, 3 rows)
# Button 14 has a double-wide (392×196) LCD display.
BUTTON_LAYOUT: List[List[int]] = [
    [1, 2, 3, 4, 5],      # row 0
    [6, 7, 8, 9, 10],     # row 1
    [11, 12, 13, 14],     # row 2 (14=double-wide)
]
TOTAL_BUTTONS = 14        # Physical buttons
TOTAL_LCD_BUTTONS = 14    # All 14 buttons have LCD displays

# Each LCD button is 196×196 pixels; button 14 is double-wide (392×196)
LCD_W = 196
LCD_H = 196
WIDE_LCD_W = LCD_W * 2          # 392 — double-wide LCD for button 14
WIDE_BUTTONS: frozenset = frozenset({14})  # buttons with double-wide LCD

# Press timing thresholds (seconds)
LONG_PRESS_SEC = 0.5    # Hold longer than this → long press
DOUBLE_PRESS_SEC = 0.35 # Two presses within this window → double press

# Linux input event key codes → button numbers
# Decoded from /proc/bus/input/devices KEY bitmask on the real device.
# Layout: landscape 5+5+4.  Key codes are assigned by the matrix wiring, not
# by visual left-to-right order.
KEY_TO_BUTTON: Dict[int, int] = {
    29: 1,  15: 2,  14: 3,  13: 4,  12: 5,   # row 0, left→right
    11: 6,  10: 7,   9: 8,   8: 9,   7: 10,  # row 1, left→right
    34: 11, 33: 12, 31: 13, 30: 14,           # row 2 (14=double-wide)
}

# Button numbers → manifest COL_ROW keys.
# The manifest format is "COL_ROW" where col 0 = leftmost, row 0 = top row.
# Bottom row physical layout: [11 col0][12 col1][13 col2][14= col3, double-wide]
# All 14 physical buttons have LCD displays.
BUTTON_TO_MANIFEST_KEY: Dict[int, str] = {
     1: "0_0",  2: "1_0",  3: "2_0",  4: "3_0",  5: "4_0",  # row 0
     6: "0_1",  7: "1_1",  8: "2_1",  9: "3_1", 10: "4_1",  # row 1
    11: "0_2", 12: "1_2", 13: "2_2", 14: "3_2",              # row 2 (14=double-wide)
}

# Device paths
DEVICE_MANIFEST = "/tmp/standalone/manifest.json"
ALL_DEVICE_MANIFESTS = [
    "/tmp/standalone/manifest.json",   # keyMode='win'
    "/tmp/standalone/manifest1.json",  # keyMode='mac'
    "/tmp/standalone/manifest2.json",  # keyMode='' (default)
]
KEY_MODE_FILE = "/userdata/keyMode"
DEVICE_IMAGES_DIR = "/tmp/standalone/Images"

# Input device
INPUT_DEVICE = "/dev/input/event0"

# struct input_event on 32-bit ARM: tv_sec(4)+tv_usec(4)+type(2)+code(2)+value(4) = 16 bytes
INPUT_EVENT_FMT = "<IIHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FMT)  # 16
