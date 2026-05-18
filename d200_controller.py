#!/usr/bin/env python3
"""
Ulanzi D200 Stream Controller
==============================
Controls the 13 LCD button displays and handles button press events via ADB.

The D200 communicates as a USB gadget with:
  - ADB shell (for filesystem access and input reading)
  - HID keyboard (Interface 2) — button presses sent as keyboard shortcuts
  - HID custom (Interface 0) — host-to-device icon protocol (proprietary)

This controller uses ADB to:
  - Read raw binary input events from /dev/input/event0
  - Push PNG icons to /tmp/standalone/Images/ on the device
  - Update /tmp/standalone/manifest2.json and restart UlanziDeckKey
    so the Qt app reloads and renders the new icons

The UlanziDeckKey Qt app (PID ~703) manages the framebuffer (/dev/fb0)
and keyboard HID gadget (/dev/hidg1). Restarting it causes a ~3-second
black-screen flash during the icon reload.

Button Layout (physical, landscape orientation):
  Row 0: [ 1] [ 2] [ 3] [ 4] [ 5]
  Row 1: [ 6] [ 7] [ 8] [ 9] [10]
  Row 2: [11*] [12] [13] [14=]    (*=no LCD display, ==double-wide LCD)

Manifest key format: "COL_ROW" where col 0 = leftmost, row 0 = top → e.g. "0_0"=top-left, "4_0"=top-right
Manifest path on device: /tmp/standalone/manifest2.json
Images path on device:   /tmp/standalone/Images/

Image folder structure (host):
  button_images/
    1/1.png  2/2.png  ...  13/13.png

Usage:
  # Generate default numbered images first:
  python3 d200_controller.py --generate-images

  # Run controller (USB ADB):
  python3 d200_controller.py

  # Run controller (WiFi ADB):
  python3 d200_controller.py --device 192.168.1.100:5555

  # Skip icon loading (events only):
  python3 d200_controller.py --no-images

  # Verbose mode:
  python3 d200_controller.py --verbose
"""

import argparse
import logging
import os
import json
import struct
import subprocess
import sys
import tempfile
import threading
import time
import base64
from io import BytesIO
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from PIL import Image, ImageDraw, ImageFont

@dataclass
class D200Config:
    images_dir: str = "./button_images"
    state_dir: str = "./state_images"
    mqtt_host: str = ""
    mqtt_user: str = ""
    mqtt_pass: str = ""
    mqtt_send_topic: str = "ulanzi/send"
    mqtt_receive_topic: str = "ulanzi/receive"
    boot_mode: str = "default"  # "default" or "state"


# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

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
# The USB version of UlanziDeckKey uses manifest.json
DEVICE_MANIFEST = "/tmp/standalone/manifest.json"
ALL_DEVICE_MANIFESTS = [
    "/tmp/standalone/manifest.json",
]
KEY_MODE_FILE = "/userdata/keyMode"
DEVICE_IMAGES_DIR = "/tmp/standalone/Images"

def disable_small_window_usb():
    """
    The Ulanzi D200 draws a clock/stats overlay (Small Window) over the right half of Button 14.
    This sends a USB HID command to set its mode to 'BACKGROUND' (2), disabling the overlay.
    """
    try:
        import hid
        import struct
    except ImportError:
        log.warning("hidapi not installed, cannot disable small window overlay.")
        return

    try:
        for d in hid.enumerate():
            if d['vendor_id'] == 0x2207 and d['product_id'] == 0x0019:
                device = hid.device()
                device.open_path(d['path'])
                device.set_nonblocking(True)
                
                data_str = "2|0|0|12:00:00|0".encode('utf-8')
                header = b'\x7c\x7c'
                cmd = struct.pack('>H', 0x0006)  # OUT_SET_SMALL_WINDOW_DATA
                length = struct.pack('<I', len(data_str))
                padded_data = data_str.ljust(1016, b'\x00')
                
                packet = header + cmd + length + padded_data
                try:
                    device.write(packet)
                except ValueError:
                    device.write(b'\x00' + packet)
                    
                log.info("Disabled Small Window overlay via USB HID.")
                device.close()
                break
    except Exception as e:
        log.debug("Failed to disable small window: %s", e)
INPUT_DEVICE = "/dev/input/event0"

# struct input_event on 32-bit ARM: tv_sec(4)+tv_usec(4)+type(2)+code(2)+value(4) = 16 bytes
INPUT_EVENT_FMT = "<IIHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FMT)  # 16

# ─── Button State ─────────────────────────────────────────────────────────────


@dataclass
class ButtonState:
    """Tracks timing state for a single button, used by press detection."""
    press_time: float = 0.0
    press_count: int = 0
    long_press_fired: bool = False
    long_timer: Optional[threading.Timer] = field(default=None, repr=False)
    eval_timer: Optional[threading.Timer] = field(default=None, repr=False)

    def cancel_timers(self):
        if self.long_timer:
            self.long_timer.cancel()
            self.long_timer = None
        if self.eval_timer:
            self.eval_timer.cancel()
            self.eval_timer = None


# ─── ADB Client ───────────────────────────────────────────────────────────────


class ADBClient:
    """
    Thin wrapper around the `adb` command-line tool.
    All device communication goes through this class.
    """

    def __init__(self, serial: Optional[str] = None):
        self.serial = serial
        self._base: List[str] = ["adb"]
        if serial:
            self._base += ["-s", serial]

    # ── Low-level helpers ─────────────────────────────────────────────────────

    def run(self, *args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
        cmd = self._base + list(args)
        log.debug("ADB: %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            **kwargs,
        )

    def shell(self, cmd: str) -> str:
        """Run a shell command on the device, return stdout (empty on error)."""
        try:
            result = self.run("shell", cmd, check=False)
            output = result.stdout.strip()
            if result.returncode != 0 and result.stderr.strip():
                log.debug("ADB shell stderr for %r: %s", cmd, result.stderr.strip())
            return output
        except FileNotFoundError:
            log.error("'adb' not found. Install with: sudo apt install adb")
            return ""
        except subprocess.CalledProcessError as e:
            log.debug("ADB shell error for %r: %s", cmd, e)
            return ""

    def push(self, local_path: str, remote_path: str) -> bool:
        """Push a local file to the device. Returns True on success."""
        try:
            result = self.run("push", local_path, remote_path, check=False)
            return result.returncode == 0
        except Exception as e:
            log.error("ADB push failed: %s", e)
            return False

    def popen(self, *args: str, **kwargs) -> subprocess.Popen:
        """Open a persistent process (e.g., for streaming getevent output)."""
        cmd = self._base + list(args)
        log.debug("ADB popen: %s", " ".join(cmd))
        return subprocess.Popen(cmd, **kwargs)

    # ── Connection checks ─────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        """Return True if the ADB device is online."""
        try:
            result = self.run("get-state", check=False)
            return result.returncode == 0 and "device" in result.stdout
        except FileNotFoundError:
            return False

    def device_info(self) -> str:
        # Try Android property first; fall back to a harmless shell command
        model = self.shell("getprop ro.product.model 2>/dev/null")
        if not model or "not found" in model:
            model = self.shell("uname -n 2>/dev/null") or "Ulanzi D200"
        return model


# ─── Display Manager ─────────────────────────────────────────────────────────


class D200Framebuffer:
    """
    Manages the 13 LCD button displays via ADB + manifest.json.

    The UlanziDeckKey Qt app owns /dev/fb0 and renders button icons based on
    /tmp/standalone/manifest2.json.  This class:
      1. Resizes icons to 196×196 PNG and pushes them to /tmp/standalone/Images/
      2. Rewrites manifest2.json with the new icon paths
      3. Kills UlanziDeckKey — the init script restarts it and it reads the
         updated manifest on boot (~3-second reload time)

    Call set_button_image() for each button, then apply() once to commit.
    """

    def __init__(self, adb: ADBClient):
        self._adb = adb
        self._pending: Dict[int, str] = {}          # button → local PNG path
        self._manifest: Dict[str, dict] = {}        # ROW_COL → manifest entry
        self._dummy_mode = False

    # ── Device probing ────────────────────────────────────────────────────────

    def probe(self) -> bool:
        """
        Verify the device manifest is accessible and load its current contents.
        Returns True if the manifest was found and loaded.

        Also reads /userdata/keyMode so we know which manifest UlanziDeckKey is
        currently using ("win"→manifest.json, "mac"→manifest1.json, ""→manifest2.json).
        apply() always writes to all manifests and resets keyMode to "", so this
        is informational only.
        """
        key_mode = self._adb.shell(f"cat {KEY_MODE_FILE} 2>/dev/null").strip()
        if key_mode != "win":
            log.info(
                "Device keyMode='%s' — setting to 'win' for USB mode.",
                key_mode,
            )

        log.info("Checking manifest on device: %s", DEVICE_MANIFEST)

        raw = self._adb.shell(f"cat {DEVICE_MANIFEST} 2>/dev/null")
        if not raw:
            # manifest2.json may not exist (device reset, first run, or custom
            # manifest was never created).  Build a minimal valid manifest so
            # apply() can still push our icons.  The file itself will be created
            # by apply() when it pushes to ALL_DEVICE_MANIFESTS.
            log.warning(
                "Manifest not found at %s — will create it on apply().",
                DEVICE_MANIFEST,
            )
            self._manifest = {}
            return True

        try:
            self._manifest = json.loads(raw)
            log.info(
                "Loaded manifest with %d button entries", len(self._manifest)
            )
            return True
        except json.JSONDecodeError as e:
            log.warning("Failed to parse manifest JSON (%s) — starting fresh.", e)
            self._manifest = {}
            return True

    # ── Image staging ─────────────────────────────────────────────────────────

    def set_button_image(self, button: int, image_path: str) -> bool:
        """
        Stage an icon update for a button (does not push yet).
        Call apply() to commit all staged changes.

        Returns True if the button has an LCD display, False otherwise.
        """
        if button not in BUTTON_TO_MANIFEST_KEY:
            log.debug("Button %d has no LCD (skipping icon staging)", button)
            return False
        if not os.path.exists(image_path):
            log.error("Image not found: %s", image_path)
            return False
        self._pending[button] = image_path
        log.debug("Staged button %d ← %s", button, os.path.basename(image_path))
        return True

    def apply(self) -> bool:
        """
        Push all staged icons to the device and restart UlanziDeckKey.

        Steps:
          1. Resize each PNG to 196×196 and push to DEVICE_IMAGES_DIR
          2. Update the in-memory manifest with the new icon paths
          3. Write manifest.json via a temp file + adb push
          4. Kill UlanziDeckKey (init script auto-restarts it)
          5. Wait ~3 s for the app to restart and render new icons

        Returns True if all pushes succeeded.
        """

        if not self._pending:
            log.debug("apply(): no pending icon changes")
            return True

        if self._dummy_mode:
            log.warning("apply(): dummy mode — changes not sent to device")
            self._pending.clear()
            return False

        success = True
        for btn, img_path in list(self._pending.items()):
            manifest_key = BUTTON_TO_MANIFEST_KEY[btn]
            device_img_name = f"btn_{btn}.png"
            device_img_path = f"{DEVICE_IMAGES_DIR}/{device_img_name}"

            try:
                target_w = WIDE_LCD_W if btn in WIDE_BUTTONS else LCD_W
                img = Image.open(img_path).convert("RGB").resize(
                    (target_w, LCD_H), Image.LANCZOS
                )
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    img.save(tmp.name, format="PNG")
                    local_tmp = tmp.name
            except Exception as e:
                log.error("Failed to prepare image for button %d: %s", btn, e)
                success = False
                continue

            try:
                if not self._adb.push(local_tmp, device_img_path):
                    log.error("adb push failed for button %d", btn)
                    success = False
                    continue
            finally:
                try:
                    os.unlink(local_tmp)
                except OSError:
                    pass

            # Update manifest entry
            self._manifest[manifest_key] = {
                "State": 0,
                "ViewParam": [{"Icon": f"Images/{device_img_name}"}],
            }
            log.info("Button %d → %s", btn, device_img_name)

        # Remove stale manifest entries whose keys no longer appear in
        # BUTTON_TO_MANIFEST_KEY (e.g. after a mapping change the old slot
        # would otherwise keep showing the wrong image).
        valid_keys = set(BUTTON_TO_MANIFEST_KEY.values())
        valid_keys.add("4_2")
        stale = [k for k in self._manifest if k not in valid_keys]
        for k in stale:
            del self._manifest[k]
            log.debug("Removed stale manifest entry: %s", k)

        # Write manifest via temp file (avoids shell quoting issues)
        # Push to ALL manifest paths so our icons show regardless of which
        # profile UlanziDeckKey has selected via /userdata/keyMode.
        manifest_json = json.dumps(self._manifest, indent=2)
        local_manifest = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as tmp:
                tmp.write(manifest_json)
                local_manifest = tmp.name

            pushed_any = False
            for manifest_path in ALL_DEVICE_MANIFESTS:
                if self._adb.push(local_manifest, manifest_path):
                    log.info("Manifest written to %s", manifest_path)
                    pushed_any = True
                else:
                    log.warning("Failed to push manifest to %s", manifest_path)
            if not pushed_any:
                log.error("Could not push manifest to any device path")
                success = False
        finally:
            if local_manifest:
                try:
                    os.unlink(local_manifest)
                except OSError:
                    pass

        # Reset keyMode to 'win' so UlanziDeckKey loads manifest.json on next start.
        self._adb.shell(f"printf 'win' > {KEY_MODE_FILE} 2>/dev/null")
        log.info("Set keyMode to 'win'")

        # Restart UlanziDeckKey to reload the manifest.
        # - Kill WatcherProcess first so it can't race-restart UlanziDeckKey.
        # - Kill WatcherProcess again after half a second in case init restarted it.
        # - Use setsid to detach UlanziDeckKey from the ADB session so it
        #   survives when the ADB shell closes.
        # - Kill WatcherProcess once more after UlanziDeckKey is up to stop any
        #   init-restarted instance from interfering.
        log.info("Restarting UlanziDeckKey to apply icon changes (~6 s)...")
        print("[INFO]  Restarting device app to apply icons (~6 s)...")
        self._adb.shell(
            "kill $(pidof WatcherProcess) 2>/dev/null;"
            " kill $(pidof UlanziDeckKey) 2>/dev/null;"
            " sleep 0.5;"
            " kill $(pidof WatcherProcess) 2>/dev/null;"  # kill any init-restarted instance
            " setsid /userdata/UlanziDeckKey -platform linuxfb >/dev/null 2>&1 &"
            " sleep 1;"
            " kill $(pidof WatcherProcess) 2>/dev/null"   # final sweep
        )
        time.sleep(4.0)  # give app time to start and render icons

        # Verify UlanziDeckKey came up; retry once if it did not.
        pid = self._adb.shell("pidof UlanziDeckKey 2>/dev/null").strip()
        if not pid:
            log.warning("UlanziDeckKey not detected after restart — retrying...")
            self._adb.shell(
                "kill $(pidof WatcherProcess) 2>/dev/null;"
                " setsid /userdata/UlanziDeckKey -platform linuxfb >/dev/null 2>&1 &"
            )
            time.sleep(3.0)
            pid = self._adb.shell("pidof UlanziDeckKey 2>/dev/null").strip()
            if not pid:
                log.error("UlanziDeckKey failed to start after retry")
                success = False
                
        if pid:
            log.info("UlanziDeckKey running (PID %s)", pid)
            # Now that it's running, disable its Small Window overlay so the right half of Button 14 is clear
            disable_small_window_usb()

        self._pending.clear()
        return success


# ─── Input Handler ────────────────────────────────────────────────────────────


class D200Input:
    """
    Reads raw binary input events from /dev/input/event0 via `adb shell cat`.

    The device uses a matrix-keypad driver.  struct input_event on 32-bit ARM
    is 16 bytes:  tv_sec(4) tv_usec(4) type(2) code(2) value(4)

    Key codes come from the hardcoded KEY_TO_BUTTON map confirmed by decoding
    the /proc/bus/input/devices bitmask on the real device.

    Press detection:
      - Single press: one press+release within DOUBLE_PRESS_SEC window
      - Double press: two press+release cycles within DOUBLE_PRESS_SEC window
      - Long press:  held for longer than LONG_PRESS_SEC
    """

    EV_KEY = 0x0001
    KEY_UP = 0
    KEY_DOWN = 1

    def __init__(self, adb: ADBClient):
        self._adb = adb
        self._states: Dict[int, ButtonState] = defaultdict(ButtonState)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None

        # Callbacks — assign before calling start()
        self.on_press: Optional[Callable[[int], None]] = None
        self.on_release: Optional[Callable[[int], None]] = None
        self.on_single_press: Optional[Callable[[int], None]] = None
        self.on_double_press: Optional[Callable[[int], None]] = None
        self.on_long_press: Optional[Callable[[int], None]] = None

    # ── Probing ───────────────────────────────────────────────────────────────

    def probe_input_devices(self) -> List[str]:
        """Verify /dev/input/event0 is accessible on the device."""
        raw = self._adb.shell("ls /dev/input/event* 2>/dev/null")
        devices = [d.strip() for d in raw.splitlines() if d.strip()]
        if INPUT_DEVICE in devices:
            log.info("Input device found: %s (%d buttons mapped)",
                     INPUT_DEVICE, len(KEY_TO_BUTTON))
        else:
            log.warning("%s not found — button events will not work", INPUT_DEVICE)
        return devices

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Start the background input listener thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop,
            daemon=True,
            name="d200-input",
        )
        self._thread.start()
        log.info("Input listener started (reading %s)", INPUT_DEVICE)

    def stop(self):
        """Stop the input listener and clean up."""
        self._running = False
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        # Cancel all pending timers
        for state in self._states.values():
            state.cancel_timers()
        log.info("Input listener stopped")

    # ── Event loop ────────────────────────────────────────────────────────────

    def _read_loop(self):
        """
        Read binary input_event structs from /dev/input/event0 via adb shell cat.
        Each struct is INPUT_EVENT_SIZE (16) bytes on 32-bit ARM.
        """
        try:
            # 'stty -onlcr' disables the PTY's NL→CR+NL output translation
            # so 0x0A bytes in the binary event stream pass through unchanged.
            self._proc = self._adb.popen(
                "shell",
                f"stty -onlcr; cat {INPUT_DEVICE}",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            buf = b""
            while self._running:
                chunk = self._proc.stdout.read(INPUT_EVENT_SIZE)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= INPUT_EVENT_SIZE:
                    event_data = buf[:INPUT_EVENT_SIZE]
                    buf = buf[INPUT_EVENT_SIZE:]
                    self._parse_event(event_data)
        except Exception as e:
            if self._running:
                log.error("Input read loop error: %s", e)

    def _parse_event(self, data: bytes):
        """Unpack one binary input_event and dispatch to press detection."""
        if len(data) < INPUT_EVENT_SIZE:
            return
        try:
            _sec, _usec, ev_type, ev_code, ev_value = struct.unpack(
                INPUT_EVENT_FMT, data
            )
        except struct.error:
            return

        if ev_type != self.EV_KEY:
            return

        button = KEY_TO_BUTTON.get(ev_code)
        if button is None:
            log.debug("Unknown key code %d (0x%02x) — ignored", ev_code, ev_code)
            return

        now = time.monotonic()
        if ev_value == self.KEY_DOWN:
            self._on_key_down(button, now)
        elif ev_value == self.KEY_UP:
            self._on_key_up(button, now)

    # ── Press detection ───────────────────────────────────────────────────────

    def _on_key_down(self, button: int, t: float):
        state = self._states[button]
        state.press_time = t
        state.long_press_fired = False

        # Cancel any previous timers
        state.cancel_timers()

        # Fire raw press callback
        if self.on_press:
            self.on_press(button)

        # Schedule long-press detection
        timer = threading.Timer(LONG_PRESS_SEC, self._fire_long_press, args=(button,))
        timer.daemon = True
        timer.start()
        state.long_timer = timer

    def _on_key_up(self, button: int, t: float):
        state = self._states[button]
        hold = t - state.press_time

        # Cancel long-press timer
        if state.long_timer:
            state.long_timer.cancel()
            state.long_timer = None

        # Fire raw release callback
        if self.on_release:
            self.on_release(button)

        # If long press already fired, skip single/double counting
        if state.long_press_fired:
            state.long_press_fired = False
            return

        # Ignore very long presses that didn't hit the threshold (edge case)
        if hold >= LONG_PRESS_SEC:
            return

        state.press_count += 1

        # Cancel any previous evaluation timer and restart the window
        if state.eval_timer:
            state.eval_timer.cancel()

        def evaluate(btn: int = button):
            s = self._states[btn]
            count = s.press_count
            s.press_count = 0
            if count >= 2:
                self._fire_double_press(btn)
            else:
                self._fire_single_press(btn)

        eval_timer = threading.Timer(DOUBLE_PRESS_SEC, evaluate)
        eval_timer.daemon = True
        eval_timer.start()
        state.eval_timer = eval_timer

    def _fire_long_press(self, button: int):
        state = self._states[button]
        state.long_press_fired = True
        state.press_count = 0  # Prevent single/double from firing after release
        msg = f"Button {button:2d}: LONG PRESS"
        log.info(msg)
        print(f"[INPUT] {msg}")
        if self.on_long_press:
            self.on_long_press(button)

    def _fire_single_press(self, button: int):
        msg = f"Button {button:2d}: single press"
        log.info(msg)
        print(f"[INPUT] {msg}")
        if self.on_single_press:
            self.on_single_press(button)

    def _fire_double_press(self, button: int):
        msg = f"Button {button:2d}: DOUBLE PRESS"
        log.info(msg)
        print(f"[INPUT] {msg}")
        if self.on_double_press:
            self.on_double_press(button)


# ─── High-Level Controller ────────────────────────────────────────────────────


class D200Controller:
    """
    High-level controller for the Ulanzi D200.

    Example usage:
        ctrl = D200Controller(images_dir="./button_images")
        if ctrl.connect():
            ctrl.load_all_button_images()

            # Custom callbacks
            ctrl.input.on_single_press = lambda btn: print(f"Pressed: {btn}")
            ctrl.input.on_long_press   = lambda btn: print(f"Long:    {btn}")
            ctrl.input.on_double_press = lambda btn: print(f"Double:  {btn}")

            ctrl.run()
    """

    def __init__(
        self,
        serial: Optional[str] = None,
        config: Optional[D200Config] = None,
    ):
        self.adb = ADBClient(serial)
        self.fb = D200Framebuffer(self.adb)
        self.input = D200Input(self.adb)
        self.config = config or D200Config()
        self.images_dir = self.config.images_dir
        self.state_dir = self.config.state_dir
        self._running = False
        self._keepalive_thread = None
        self.mqtt_client = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Connect to the device and initialise hardware.
        Returns True on success, False if no device is available.
        """
        if not self.adb.is_connected():
            log.error(
                "No ADB device connected.\n"
                "  • Please plug in device and enable ADB debugging"
            )
            print(
                "\n[ERROR] No ADB device connected.\n"
                "        Please enable ADB debugging and plug in the D200\n"
            )
            return False

        model = self.adb.device_info()
        log.info("Connected to: %s", model)
        print(f"[INFO]  Connected to device: {model}")

        disable_small_window_usb()

        self.fb.probe()
        self.input.probe_input_devices()
        return True

    def load_all_button_images(self) -> int:
        """
        Stage and apply images for all LCD buttons.
        Returns the number of images successfully staged.
        """
        log.info("Loading button images")
        count = 0
        for btn in sorted(BUTTON_TO_MANIFEST_KEY.keys()):
            img_path = None
            if self.config.boot_mode == "state":
                state_path = os.path.join(self.state_dir, str(btn), f"{btn}.png")
                if os.path.exists(state_path):
                    img_path = state_path
            
            if not img_path:
                img_path = os.path.join(self.images_dir, str(btn), f"{btn}.png")

            if not os.path.exists(img_path):
                log.warning("Image not found for button %d: %s", btn, img_path)
                continue
            if self.fb.set_button_image(btn, img_path):
                count += 1
        log.info("Staged %d/%d button images", count, TOTAL_LCD_BUTTONS)
        print(f"[INFO]  Pushing {count}/{TOTAL_LCD_BUTTONS} button images...")
        self.fb.apply()
        return count

    def set_button_image(self, button: int, image_path: str, apply: bool = False) -> bool:
        """
        Stage a single button's image.  Pass apply=True to push immediately.
        """
        ok = self.fb.set_button_image(button, image_path)
        if ok and apply:
            self.fb.apply()
        return ok

    # ── MQTT ──────────────────────────────────────────────────────────────────

    def _setup_mqtt(self):
        if not mqtt or not self.config.mqtt_host:
            return
        
        self.mqtt_client = mqtt.Client()
        if self.config.mqtt_user:
            self.mqtt_client.username_pw_set(self.config.mqtt_user, self.config.mqtt_pass)
        
        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                log.info("Connected to MQTT broker")
                if self.config.mqtt_receive_topic:
                    client.subscribe(self.config.mqtt_receive_topic)
            else:
                log.error(f"Failed to connect to MQTT broker, return code {rc}")
                
        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode('utf-8'))
                btn = payload.get("button")
                img_b64 = payload.get("image")
                if btn is not None and img_b64:
                    self._handle_mqtt_image(int(btn), img_b64)
            except Exception as e:
                log.error(f"Error handling MQTT message: {e}")

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_message = on_message
        
        try:
            self.mqtt_client.connect(self.config.mqtt_host)
            self.mqtt_client.loop_start()
        except Exception as e:
            log.error(f"MQTT Connect exception: {e}")

    def _handle_mqtt_image(self, btn: int, img_b64: str):
        if btn not in BUTTON_TO_MANIFEST_KEY:
            log.warning(f"MQTT received image for invalid button {btn}")
            return
            
        try:
            img_data = base64.b64decode(img_b64)
            img = Image.open(BytesIO(img_data)).convert("RGB")
            
            target_w = WIDE_LCD_W if btn in WIDE_BUTTONS else LCD_W
            img = img.resize((target_w, LCD_H), Image.LANCZOS)
            
            btn_dir = os.path.join(self.state_dir, str(btn))
            os.makedirs(btn_dir, exist_ok=True)
            img_path = os.path.join(btn_dir, f"{btn}.png")
            img.save(img_path, format="PNG")
            
            self.set_button_image(btn, img_path, apply=True)
            log.info(f"Updated image for button {btn} via MQTT")
        except Exception as e:
            log.error(f"Failed to process MQTT image for button {btn}: {e}")

    def _publish_event(self, btn: int, action: str):
        if self.mqtt_client and self.config.mqtt_send_topic:
            payload = json.dumps({"button": btn, "action": action})
            self.mqtt_client.publish(self.config.mqtt_send_topic, payload)

    # ── Event loop ────────────────────────────────────────────────────────────

    def run(self):
        """
        Start the controller's main loop.
        Blocks until Ctrl+C is pressed.
        Registers default callbacks that print button events to the console.
        """
        self._running = True

        # Register default print callbacks (user can override these before calling run())
        if self.input.on_press is None:
            def handle_press(btn):
                print(f"[INPUT] Button {btn:2d}: ↓ pressed")
                self._publish_event(btn, "down")
            self.input.on_press = handle_press
            
        if self.input.on_release is None:
            def handle_release(btn):
                print(f"[INPUT] Button {btn:2d}: ↑ released")
                self._publish_event(btn, "up")
            self.input.on_release = handle_release
            
        if self.input.on_single_press is None:
            def handle_single(btn):
                print(f"[INPUT] Button {btn:2d}: single press")
                self._publish_event(btn, "single_press")
            self.input.on_single_press = handle_single
            
        if self.input.on_double_press is None:
            def handle_double(btn):
                print(f"[INPUT] Button {btn:2d}: DOUBLE press")
                self._publish_event(btn, "double_press")
            self.input.on_double_press = handle_double
            
        if self.input.on_long_press is None:
            def handle_long(btn):
                print(f"[INPUT] Button {btn:2d}: LONG press")
                self._publish_event(btn, "long_press")
            self.input.on_long_press = handle_long

        self._setup_mqtt()
        self.input.start()
        self._print_layout()

        # Start the keep-alive thread to prevent the Ulanzi device from timing out
        self._keepalive_thread = threading.Thread(
            target=self._keep_alive_loop,
            daemon=True,
            name="d200-keepalive"
        )
        self._keepalive_thread.start()

        try:
            while self._running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n[INFO]  Stopping…")
        finally:
            self.stop()

    def stop(self):
        """Stop the controller cleanly."""
        self._running = False
        self.input.stop()
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=1.0)

    def _keep_alive_loop(self):
        """
        The Ulanzi device expects 'Host information' periodically. If it doesn't receive
        a USB HID packet for ~10 seconds, it triggers a reconnection and breaks the UI.
        This loop sends the background mode packet every 8 seconds to keep it alive.
        """
        while self._running:
            for _ in range(80):  # 8 seconds, check self._running frequently
                if not self._running:
                    return
                time.sleep(0.1)
            # Send keep-alive packet (also reinforces the hidden overlay state)
            disable_small_window_usb()

    @staticmethod
    def _print_layout():
        print()
        print("Ulanzi D200 — controller running. Press Ctrl+C to exit.")
        print()
        print("Button layout (5+5+4, 13 LCD buttons; * no LCD, = double-wide LCD):")
        for i, row in enumerate(BUTTON_LAYOUT):
            labels = "  ".join(
                f"[{btn:2d}=]" if btn in WIDE_BUTTONS else
                (f"[{btn:2d}*]" if btn not in BUTTON_TO_MANIFEST_KEY else f"[{btn:2d}]")
                for btn in row
            )
            print(f"  Row {i}:  {labels}")
        print("  (* = no LCD display, = = double-wide LCD)")
        print()
        print("Waiting for button events…")
        print()


# ─── Image Generator ──────────────────────────────────────────────────────────


# Each button gets a distinct background color
BUTTON_COLORS = [
    "#E74C3C", "#E67E22", "#F1C40F", "#2ECC71", "#1ABC9C",
    "#3498DB", "#9B59B6", "#E91E63", "#FF5722", "#795548",
    "#607D8B", "#00BCD4", "#8BC34A", "#FF9800",
]


def generate_button_images(base_dir: str):
    """
    Generate numbered button images for all 13 LCD buttons.

    Creates: {base_dir}/{N}/{N}.png — 196×196 colored tile with the
    button number rendered in white.
    """
    print(f"Generating button images in: {base_dir}")

    # Try to find a reasonably large font
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    font = None
    for path in font_candidates:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, 80)
                log.debug("Using font: %s", path)
                break
            except Exception:
                continue
    if font is None:
        log.warning("No TrueType font found — using default bitmap font")
        font = ImageFont.load_default()

    for btn in sorted(BUTTON_TO_MANIFEST_KEY.keys()):
        btn_dir = os.path.join(base_dir, str(btn))
        os.makedirs(btn_dir, exist_ok=True)

        img_path = os.path.join(btn_dir, f"{btn}.png")

        # Colored background — button 14 uses double-wide canvas
        color = BUTTON_COLORS[(btn - 1) % len(BUTTON_COLORS)]
        w = WIDE_LCD_W if btn in WIDE_BUTTONS else LCD_W
        img = Image.new("RGB", (w, LCD_H), color)
        draw = ImageDraw.Draw(img)

        # Center the number
        text = str(btn)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = (w - tw) // 2 - bbox[0]
        y = (LCD_H - th) // 2 - bbox[1]

        # Drop shadow + white text
        draw.text((x + 3, y + 3), text, fill="black", font=font)
        draw.text((x, y), text, fill="white", font=font)

        img.save(img_path)
        print(f"  Created: {img_path}  ({w}x{LCD_H})")

    print(f"\nDone — {TOTAL_LCD_BUTTONS} images created.")


# ─── CLI Entry Point ──────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Ulanzi D200 Stream Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Step 1 — generate default numbered button images:
  python3 d200_controller.py --generate-images

  # Step 2 — run the controller:
  python3 d200_controller.py
""",
    )
    parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Directory to save/load state images",
    )
    parser.add_argument(
        "--config", "-c",
        metavar="FILE",
        default="config.json",
        help="Path to configuration file",
    )
    parser.add_argument(
        "--mqtt-host",
        help="MQTT broker host",
    )
    parser.add_argument(
        "--boot-mode",
        choices=["default", "state"],
        help="Boot mode for images",
    )
    parser.add_argument(
        "--images-dir", "-i",
        metavar="DIR",
        help="Directory containing button images",
    )
    parser.add_argument(
        "--generate-images",
        action="store_true",
        help="Generate default numbered button images then exit",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Skip loading button images (events only)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Load Configuration ────────────────────────────────────────────────────
    cfg_data = {}
    if os.path.exists(args.config):
        with open(args.config, "r") as f:
            cfg_data = json.load(f)
            
    # CLI args override file config
    config = D200Config()
    if args.images_dir: config.images_dir = args.images_dir
    elif "images_dir" in cfg_data: config.images_dir = cfg_data["images_dir"]
    
    if args.state_dir: config.state_dir = args.state_dir
    elif "state_dir" in cfg_data: config.state_dir = cfg_data["state_dir"]
    
    if args.mqtt_host: config.mqtt_host = args.mqtt_host
    elif "mqtt_host" in cfg_data: config.mqtt_host = cfg_data["mqtt_host"]
    
    if "mqtt_user" in cfg_data: config.mqtt_user = cfg_data["mqtt_user"]
    if "mqtt_pass" in cfg_data: config.mqtt_pass = cfg_data["mqtt_pass"]
    if "mqtt_send_topic" in cfg_data: config.mqtt_send_topic = cfg_data["mqtt_send_topic"]
    if "mqtt_receive_topic" in cfg_data: config.mqtt_receive_topic = cfg_data["mqtt_receive_topic"]
    
    if args.boot_mode: config.boot_mode = args.boot_mode
    elif "boot_mode" in cfg_data: config.boot_mode = cfg_data["boot_mode"]

    # ── Generate images mode ──────────────────────────────────────────────────
    if args.generate_images:
        generate_button_images(config.images_dir)
        return

    # ── Controller mode ───────────────────────────────────────────────────────
    ctrl = D200Controller(config=config)

    if not ctrl.connect():
        sys.exit(1)

    if not args.no_images:
        ctrl.load_all_button_images()

    ctrl.run()


if __name__ == "__main__":
    main()
