#!/usr/bin/env python3
"""
Test suite for the ulanzi_d200 package.
Tests all logic that can be validated without a physical device:
  - Key mapping constants (KEY_TO_BUTTON, BUTTON_TO_MANIFEST_KEY)
  - Button image generation and folder structure
  - Press-type detection (single / double / long)
  - Binary input_event parsing (_parse_event)
  - Manifest probe / set_button_image API
  - ADBClient interface
"""

import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from ulanzi_d200 import (
    ADBClient,
    BUTTON_TO_MANIFEST_KEY,
    ButtonState,
    D200Config,
    D200Controller,
    D200Framebuffer,
    D200Input,
    DOUBLE_PRESS_SEC,
    INPUT_EVENT_FMT,
    INPUT_EVENT_SIZE,
    KEY_TO_BUTTON,
    LCD_H,
    LCD_W,
    LONG_PRESS_SEC,
    TOTAL_BUTTONS,
    TOTAL_LCD_BUTTONS,
    WIDE_BUTTONS,
    WIDE_LCD_W,
    generate_button_images,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def banner(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def make_event(ev_type: int, ev_code: int, ev_value: int) -> bytes:
    """Pack a Linux input_event struct (32-bit ARM, 16 bytes)."""
    return struct.pack(INPUT_EVENT_FMT, 0, 0, ev_type, ev_code, ev_value)


EV_KEY = 0x0001
EV_SYN = 0x0000


# ─── Key Mapping Tests ────────────────────────────────────────────────────────


class TestKeyMappings(unittest.TestCase):

    def test_key_to_button_count(self):
        """KEY_TO_BUTTON should map exactly TOTAL_BUTTONS keys."""
        self.assertEqual(len(KEY_TO_BUTTON), TOTAL_BUTTONS)

    def test_button_to_manifest_count(self):
        """BUTTON_TO_MANIFEST_KEY should map exactly TOTAL_LCD_BUTTONS buttons."""
        self.assertEqual(len(BUTTON_TO_MANIFEST_KEY), TOTAL_LCD_BUTTONS)

    def test_button_11_is_in_manifest(self):
        """Button 11 is a valid LCD button."""
        self.assertIn(11, BUTTON_TO_MANIFEST_KEY)

    def test_button_14_is_in_manifest(self):
        """Button 14 (double-wide LCD) must have a manifest key."""
        self.assertIn(14, BUTTON_TO_MANIFEST_KEY)

    def test_lcd_buttons_all_in_manifest(self):
        """Buttons 1–14 must all have manifest keys."""
        for btn in range(1, TOTAL_BUTTONS + 1):
            self.assertIn(btn, BUTTON_TO_MANIFEST_KEY,
                          f"Button {btn} missing from BUTTON_TO_MANIFEST_KEY")

    def test_manifest_key_format(self):
        """All manifest keys must match COL_ROW format."""
        for btn, key in BUTTON_TO_MANIFEST_KEY.items():
            parts = key.split("_")
            self.assertEqual(len(parts), 2, f"Button {btn}: bad key format {key!r}")
            self.assertTrue(parts[0].isdigit() and parts[1].isdigit(),
                            f"Button {btn}: non-numeric COL_ROW {key!r}")

    def test_key_to_button_values_unique(self):
        """Each button number should appear at most once in KEY_TO_BUTTON."""
        buttons = list(KEY_TO_BUTTON.values())
        self.assertEqual(len(buttons), len(set(buttons)),
                         "Duplicate button numbers in KEY_TO_BUTTON")

    def test_known_key_codes(self):
        """Spot-check specific key codes confirmed from the real device."""
        self.assertEqual(KEY_TO_BUTTON[29],  1)   # row 0, leftmost
        self.assertEqual(KEY_TO_BUTTON[7],  10)   # row 1, rightmost
        self.assertEqual(KEY_TO_BUTTON[34], 11)   # row 2, leftmost
        self.assertEqual(KEY_TO_BUTTON[30], 14)   # row 2, double-wide


# ─── Image Generation Tests ───────────────────────────────────────────────────


class TestGenerateButtonImages(unittest.TestCase):

    def test_all_images_created(self):
        """generate_button_images() creates exactly TOTAL_LCD_BUTTONS images."""
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_button_images(tmpdir)
            for btn in sorted(BUTTON_TO_MANIFEST_KEY.keys()):
                path = os.path.join(tmpdir, str(btn), f"{btn}.png")
                self.assertTrue(os.path.exists(path), f"Missing: {path}")

    def test_image_dimensions(self):
        """Generated images match expected dimensions (button 14 is double-wide)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_button_images(tmpdir)
            for btn in sorted(BUTTON_TO_MANIFEST_KEY.keys()):
                path = os.path.join(tmpdir, str(btn), f"{btn}.png")
                with Image.open(path) as img:
                    expected_w = WIDE_LCD_W if btn in WIDE_BUTTONS else LCD_W
                    self.assertEqual(img.size, (expected_w, LCD_H),
                                     f"Button {btn}: expected {expected_w}×{LCD_H}, got {img.size}")

    def test_folder_structure(self):
        """Each LCD button has its own sub-folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_button_images(tmpdir)
            for btn in sorted(BUTTON_TO_MANIFEST_KEY.keys()):
                btn_dir = os.path.join(tmpdir, str(btn))
                self.assertTrue(os.path.isdir(btn_dir),
                                f"Missing directory: {btn_dir}")


# ─── Binary Event Parsing Tests ───────────────────────────────────────────────


class TestBinaryEventParsing(unittest.TestCase):
    """Tests for D200Input._parse_event() with real binary input_event structs."""

    def setUp(self):
        mock_adb = MagicMock(spec=ADBClient)
        self.inp = D200Input(mock_adb)

    def test_event_struct_size(self):
        """INPUT_EVENT_SIZE must be 16 bytes on 32-bit ARM."""
        self.assertEqual(INPUT_EVENT_SIZE, 16)

    def test_key_down_fires_on_press(self):
        fired = []
        self.inp.on_press = fired.append
        self.inp._parse_event(make_event(EV_KEY, 29, 1))
        self.assertEqual(fired, [1])

    def test_key_up_fires_on_release(self):
        fired = []
        self.inp.on_press = lambda _: None
        self.inp.on_release = fired.append
        self.inp._parse_event(make_event(EV_KEY, 29, 1))
        self.inp._parse_event(make_event(EV_KEY, 29, 0))
        self.assertEqual(fired, [1])

    def test_syn_event_ignored(self):
        """EV_SYN (type 0) events must not trigger any callback."""
        fired = []
        self.inp.on_press = fired.append
        self.inp._parse_event(make_event(EV_SYN, 0, 0))
        self.assertEqual(fired, [])

    def test_unknown_key_code_ignored(self):
        """Unknown key codes (not in KEY_TO_BUTTON) must be silently ignored."""
        fired = []
        self.inp.on_press = fired.append
        self.inp._parse_event(make_event(EV_KEY, 0x9999, 1))
        self.assertEqual(fired, [])

    def test_all_known_keys_fire(self):
        """Every entry in KEY_TO_BUTTON must fire the correct button number."""
        for code, expected_btn in KEY_TO_BUTTON.items():
            fired = []
            self.inp.on_press = fired.append
            self.inp._parse_event(make_event(EV_KEY, code, 1))
            self.assertEqual(fired, [expected_btn],
                             f"Key code {code} expected button {expected_btn}")

    def test_truncated_data_ignored(self):
        """Events shorter than INPUT_EVENT_SIZE must not crash or fire."""
        fired = []
        self.inp.on_press = fired.append
        self.inp._parse_event(b"\x00" * 8)
        self.inp._parse_event(b"")
        self.assertEqual(fired, [])

    def test_button_11_fires_on_press(self):
        """Button 11 (key code 34) fires on_press."""
        fired = []
        self.inp.on_press = fired.append
        self.inp._parse_event(make_event(EV_KEY, 34, 1))
        self.assertEqual(fired, [11])


# ─── Press Detection Tests ────────────────────────────────────────────────────


class TestPressDetection(unittest.TestCase):
    """
    Tests for single / double / long press detection.
    Uses real timers but with small delays to keep the test suite fast.
    """

    KEY_CODE = 29  # button 1

    def setUp(self):
        mock_adb = MagicMock(spec=ADBClient)
        self.inp = D200Input(mock_adb)

        self.single_events = []
        self.double_events = []
        self.long_events = []

        self.inp.on_single_press = self.single_events.append
        self.inp.on_double_press = self.double_events.append
        self.inp.on_long_press   = self.long_events.append
        self.inp.on_press   = lambda _: None
        self.inp.on_release = lambda _: None

    def _press(self, value: int):
        self.inp._parse_event(make_event(EV_KEY, self.KEY_CODE, value))

    def _click(self):
        self._press(1)
        time.sleep(0.05)
        self._press(0)

    def test_single_press(self):
        self._click()
        time.sleep(DOUBLE_PRESS_SEC + 0.1)
        self.assertEqual(self.single_events, [1], "Expected exactly one single-press event")
        self.assertEqual(self.double_events, [])

    def test_double_press(self):
        self._click()
        time.sleep(0.05)
        self._click()
        time.sleep(DOUBLE_PRESS_SEC + 0.1)
        self.assertEqual(self.double_events, [1], "Expected exactly one double-press event")
        self.assertEqual(self.single_events, [])

    def test_long_press(self):
        self._press(1)
        time.sleep(LONG_PRESS_SEC + 0.15)
        self.assertEqual(self.long_events, [1], "Expected exactly one long-press event")
        self._press(0)
        time.sleep(DOUBLE_PRESS_SEC + 0.1)
        self.assertEqual(self.single_events, [])
        self.assertEqual(self.double_events, [])

    def test_single_does_not_fire_after_long_press(self):
        """After a long press the release must not trigger single press."""
        self._press(1)
        time.sleep(LONG_PRESS_SEC + 0.15)
        self._press(0)
        time.sleep(DOUBLE_PRESS_SEC + 0.1)
        self.assertEqual(self.single_events, [], "Single press fired after long press!")

    def test_different_buttons_independent(self):
        """Events on different buttons don't interfere with each other."""
        mock_adb = MagicMock(spec=ADBClient)
        inp = D200Input(mock_adb)
        events = []
        inp.on_single_press = events.append
        inp.on_press = inp.on_release = lambda _: None

        inp._parse_event(make_event(EV_KEY, 29, 1))
        time.sleep(0.05)
        inp._parse_event(make_event(EV_KEY, 29, 0))

        inp._parse_event(make_event(EV_KEY, 15, 1))
        time.sleep(0.05)
        inp._parse_event(make_event(EV_KEY, 15, 0))

        time.sleep(DOUBLE_PRESS_SEC + 0.1)
        self.assertIn(1, events)
        self.assertIn(2, events)


# ─── Manifest API Tests ───────────────────────────────────────────────────────


class TestManifestAPI(unittest.TestCase):

    def _make_mock_adb(self, manifest_json: str = "") -> MagicMock:
        mock = MagicMock(spec=ADBClient)
        mock.shell.return_value = manifest_json
        return mock

    def test_probe_returns_true_on_empty(self):
        """probe() returns True even when manifest is missing."""
        adb = self._make_mock_adb("")
        fb = D200Framebuffer(adb)
        self.assertTrue(fb.probe())
        self.assertEqual(fb._manifest, {})

    def test_probe_returns_true_on_valid_json(self):
        """probe() returns True and stores manifest when JSON is valid."""
        manifest = '{"0_0": {"State": 0, "ViewParam": []}}'
        adb = self._make_mock_adb(manifest)
        fb = D200Framebuffer(adb)
        self.assertTrue(fb.probe())
        self.assertIn("0_0", fb._manifest)

    def test_probe_returns_true_on_bad_json(self):
        """probe() returns True even on malformed JSON (starts with empty manifest)."""
        adb = self._make_mock_adb("{bad json}")
        fb = D200Framebuffer(adb)
        self.assertTrue(fb.probe())
        self.assertEqual(fb._manifest, {})

    def test_set_button_image_stages_valid_button(self):
        """set_button_image() returns True and stages the path for LCD buttons."""
        adb = self._make_mock_adb()
        fb = D200Framebuffer(adb)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = os.path.join(tmpdir, "btn.png")
            Image.new("RGB", (196, 196)).save(tmp_path)
            self.assertTrue(fb.set_button_image(1, tmp_path))
            self.assertIn(1, fb._pending)

    def test_set_button_image_accepts_button_11(self):
        """set_button_image() returns True for button 11."""
        adb = self._make_mock_adb()
        fb = D200Framebuffer(adb)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = os.path.join(tmpdir, "btn.png")
            Image.new("RGB", (196, 196)).save(tmp_path)
            self.assertTrue(fb.set_button_image(11, tmp_path))
            self.assertIn(11, fb._pending)

    def test_set_button_image_rejects_missing_file(self):
        """set_button_image() returns False when the image file doesn't exist."""
        adb = self._make_mock_adb()
        fb = D200Framebuffer(adb)
        self.assertFalse(fb.set_button_image(1, "/nonexistent/path/button.png"))
        self.assertNotIn(1, fb._pending)

    def test_apply_noop_when_no_pending(self):
        """apply() returns True immediately with no staged changes."""
        adb = self._make_mock_adb()
        fb = D200Framebuffer(adb)
        self.assertTrue(fb.apply())

    def test_apply_returns_false_in_dummy_mode(self):
        """apply() returns False and clears pending in dummy mode."""
        adb = self._make_mock_adb()
        fb = D200Framebuffer(adb)
        fb._dummy_mode = True
        fb._pending[1] = "/some/path.png"
        self.assertFalse(fb.apply())
        self.assertEqual(fb._pending, {})


# ─── ButtonState Tests ────────────────────────────────────────────────────────


class TestButtonState(unittest.TestCase):

    def test_cancel_timers_no_crash_when_none(self):
        """cancel_timers() is safe when timers are None."""
        ButtonState().cancel_timers()

    def test_cancel_timers_cancels_both(self):
        long_timer = MagicMock()
        eval_timer = MagicMock()
        state = ButtonState(long_timer=long_timer, eval_timer=eval_timer)
        state.cancel_timers()
        long_timer.cancel.assert_called_once()
        eval_timer.cancel.assert_called_once()
        self.assertIsNone(state.long_timer)
        self.assertIsNone(state.eval_timer)


# ─── ADBClient Tests ──────────────────────────────────────────────────────────


class TestADBClient(unittest.TestCase):

    def test_serial_added_to_command(self):
        """ADB serial flag is inserted correctly."""
        adb = ADBClient(serial="test_serial")
        self.assertIn("-s", adb._base)
        self.assertIn("test_serial", adb._base)

    def test_no_serial_by_default(self):
        adb = ADBClient()
        self.assertEqual(adb._base, ["adb"])

    def test_is_connected_false_when_adb_missing(self):
        """Returns False gracefully when adb is not in PATH."""
        adb = ADBClient()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(adb.is_connected())

    def test_shell_returns_empty_on_error(self):
        """shell() returns empty string when the command fails."""
        adb = ADBClient()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"
        with patch("subprocess.run", return_value=mock_result):
            self.assertEqual(adb.shell("some_command"), "")


# ─── D200Controller Tests ─────────────────────────────────────────────────────


class TestD200Controller(unittest.TestCase):

    def test_no_crash_on_missing_device(self):
        """connect() returns False when ADB is unavailable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ctrl = D200Controller(config=D200Config(images_dir=tmpdir))
            ctrl.adb.is_connected = MagicMock(return_value=False)
            self.assertFalse(ctrl.connect())

    def test_load_all_button_images_dummy_mode(self):
        """load_all_button_images() returns 0 when the images directory is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ctrl = D200Controller(config=D200Config(images_dir=tmpdir))
            ctrl.adb.is_connected = MagicMock(return_value=True)
            ctrl.adb.shell = MagicMock(return_value="Ulanzi D200")
            ctrl.fb._dummy_mode = True
            self.assertEqual(ctrl.load_all_button_images(), 0)

    def test_load_all_button_images_with_files(self):
        """load_all_button_images() stages TOTAL_LCD_BUTTONS images when all are present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_button_images(tmpdir)
            ctrl = D200Controller(config=D200Config(images_dir=tmpdir))
            ctrl.fb._dummy_mode = True
            self.assertEqual(ctrl.load_all_button_images(), TOTAL_LCD_BUTTONS)


# ─── Runner ───────────────────────────────────────────────────────────────────


def run_all_tests():
    banner("Ulanzi D200 Controller — Test Suite")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestKeyMappings,
        TestGenerateButtonImages,
        TestBinaryEventParsing,
        TestPressDetection,
        TestManifestAPI,
        TestButtonState,
        TestADBClient,
        TestD200Controller,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)

    print()
    total = result.testsRun
    failures = len(result.failures) + len(result.errors)
    passed = total - failures
    print(f"Results: {passed}/{total} passed", end="")
    if failures:
        print(f"  ({failures} failed)")
    else:
        print(" — all tests passed!")

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
