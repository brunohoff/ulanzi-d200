"""
Image generator — creates numbered PNG tiles for all Ulanzi D200 buttons.

Button 14 uses a double-wide canvas (392×196) to match its wider LCD panel.
"""

import logging
import os
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

from .constants import (
    BUTTON_TO_MANIFEST_KEY,
    LCD_H,
    LCD_W,
    TOTAL_LCD_BUTTONS,
    WIDE_BUTTONS,
    WIDE_LCD_W,
)

log = logging.getLogger(__name__)

# Each button gets a distinct background color (1–14 cycle)
BUTTON_COLORS: List[str] = [
    "#E74C3C", "#E67E22", "#F1C40F", "#2ECC71", "#1ABC9C",
    "#3498DB", "#9B59B6", "#E91E63", "#FF5722", "#795548",
    "#607D8B", "#00BCD4", "#8BC34A", "#FF9800",
]

# Candidate TrueType font paths (local first, then system)
_FONT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts", "DejaVuSans-Bold.ttf"),
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Return the best available TrueType font at *size*, or the default bitmap font."""
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                log.debug("Using font: %s", path)
                return font
            except Exception:
                continue
    log.warning("No TrueType font found — using default bitmap font")
    return ImageFont.load_default()


def generate_button_images(base_dir: str, font_size: int = 80) -> None:
    """
    Generate numbered PNG tiles for all 14 LCD buttons.

    Layout on disk::

        {base_dir}/
            1/1.png     196×196
            2/2.png     196×196
            ...
            13/13.png   196×196
            14/14.png   392×196  ← double-wide for button 14

    Args:
        base_dir:  Root directory where per-button sub-folders are created.
        font_size: Point size for the number label (default 80).
    """
    print(f"Generating button images in: {base_dir}")
    for btn in sorted(BUTTON_TO_MANIFEST_KEY.keys()):
        btn_dir = os.path.join(base_dir, str(btn))
        os.makedirs(btn_dir, exist_ok=True)

        img_path = os.path.join(btn_dir, f"{btn}.png")
        color = BUTTON_COLORS[(btn - 1) % len(BUTTON_COLORS)]

        # Button 14 is double-wide, use a larger font
        w = WIDE_LCD_W if btn in WIDE_BUTTONS else LCD_W
        h = LCD_H
        # Scale font size for button 14
        btn_font_size = font_size
        if btn in WIDE_BUTTONS:
            btn_font_size = int(font_size * (WIDE_LCD_W / LCD_W) * 0.9)  # 0.9 fudge factor for padding
        font = _load_font(btn_font_size)

        img = Image.new("RGB", (w, h), color)
        draw = ImageDraw.Draw(img)

        # Center the number label
        text = str(btn)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = (w - tw) // 2 - bbox[0]
        y = (h - th) // 2 - bbox[1]

        # Draw drop shadow and white number (only once)
        draw.text((x + 3, y + 3), text, fill="black", font=font)
        draw.text((x, y), text, fill="white", font=font)

        img.save(img_path)
        print(f"  [{btn:2d}]  {img_path}  ({w}×{h})")

    print(f"\nDone — {TOTAL_LCD_BUTTONS} images created.")
