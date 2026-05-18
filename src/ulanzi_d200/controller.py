"""
High-level controller — wires ADB, framebuffer, input, and MQTT together.
"""

import base64
import json
import logging
import os
import threading
import time
from io import BytesIO
from typing import Optional
import itertools

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None  # type: ignore[assignment]

from PIL import Image

from .adb_client import ADBClient
from .constants import (
    BUTTON_LAYOUT,
    BUTTON_TO_MANIFEST_KEY,
    KEY_MODE_FILE,
    LCD_H,
    LCD_W,
    TOTAL_LCD_BUTTONS,
    WIDE_BUTTONS,
    WIDE_LCD_W,
)
from .framebuffer import D200Framebuffer, send_hid_keepalive
from .input_handler import D200Input
from .models import D200Config

log = logging.getLogger(__name__)


class D200Controller:
    """
    High-level controller for the Ulanzi D200.

    Example usage::

        ctrl = D200Controller(config=D200Config(images_dir="./button_images"))
        if ctrl.connect():
            ctrl.load_all_button_images()
            ctrl.input.on_single_press = lambda btn: print(f"Pressed: {btn}")
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
        self._keepalive_thread: Optional[threading.Thread] = None
        self._devlog_thread: Optional[threading.Thread] = None
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

        self.fb.probe()
        self.input.probe_input_devices()
        return True

    # ── Image loading ─────────────────────────────────────────────────────────

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
        """Stage a single button's image.  Pass apply=True to push immediately."""
        ok = self.fb.set_button_image(button, image_path)
        if ok and apply:
            self.fb.apply()
        return ok

    # ── MQTT ──────────────────────────────────────────────────────────────────

    def _setup_mqtt(self) -> None:
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
                log.error("Failed to connect to MQTT broker, return code %d", rc)

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode('utf-8'))
                btn = payload.get("button")
                img_b64 = payload.get("image")
                if btn is not None and img_b64:
                    self._handle_mqtt_image(int(btn), img_b64)
            except Exception as e:
                log.error("Error handling MQTT message: %s", e)

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_message = on_message

        try:
            self.mqtt_client.connect(self.config.mqtt_host)
            self.mqtt_client.loop_start()
        except Exception as e:
            log.error("MQTT Connect exception: %s", e)

    def _handle_mqtt_image(self, btn: int, img_b64: str) -> None:
        if btn not in BUTTON_TO_MANIFEST_KEY:
            log.warning("MQTT received image for invalid button %d", btn)
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
            log.info("Updated image for button %d via MQTT", btn)
        except Exception as e:
            log.error("Failed to process MQTT image for button %d: %s", btn, e)

    def _publish_event(self, btn: int, action: str) -> None:
        if self.mqtt_client and self.config.mqtt_send_topic:
            payload = json.dumps({"button": btn, "action": action})
            self.mqtt_client.publish(self.config.mqtt_send_topic, payload)

    # ── Main event loop ───────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Start the controller's main loop.
        Blocks until Ctrl+C is pressed.
        """
        self._running = True

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

        self._keepalive_thread = threading.Thread(
            target=self._keep_alive_loop,
            daemon=True,
            name="d200-keepalive",
        )
        self._keepalive_thread.start()

        self._devlog_thread = threading.Thread(
            target=self._device_log_loop,
            daemon=True,
            name="d200-devlog",
        )
        self._devlog_thread.start()

        try:
            while self._running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n[INFO]  Stopping…")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the controller cleanly."""
        self._running = False
        self.input.stop()
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=1.0)
        if self._devlog_thread:
            self._devlog_thread.join(timeout=1.0)

    def _device_log_loop(self) -> None:
        """
        Polls /userdata/logs/log.txt on the device every 5 seconds and
        forwards new lines to the Python logger.

        This helps correlate device-side events (UlanziDeckKey, WatcherProcess)
        with host-side events (HID writes, ADB reconnects).

        Noisy lines ('receiveData first.size() <= 8') are counted and
        summarised rather than repeated verbatim, so they don't drown out
        other events.  Any NEW message type always appears immediately.
        """
        DEVICE_LOG = "/userdata/logs/log.txt"
        POLL_INTERVAL = 5
        # Lines with these substrings are treated as noise and summarised.
        NOISE_PATTERNS = (
            "receiveData first.size() <= 8",
        )
        dlog = logging.getLogger("d200.device")

        last_line = 0          # last line number we have already emitted
        noise_counts: dict = {}  # pattern → count since last non-noise line

        # Get starting line count so we don't replay history on first connect.
        raw = self.adb.shell(f"wc -l {DEVICE_LOG} 2>/dev/null")
        try:
            last_line = int(raw.split()[0])
            dlog.debug("Device log starts at line %d", last_line)
        except (ValueError, IndexError):
            last_line = 0

        while self._running:
            # Sleep in short increments so stop() is responsive.
            for _ in range(POLL_INTERVAL * 10):
                if not self._running:
                    return
                time.sleep(0.1)

            # Fetch only new lines.
            new_text = self.adb.shell(
                f"awk 'NR > {last_line}' {DEVICE_LOG} 2>/dev/null"
            )
            if not new_text:
                continue

            lines = new_text.splitlines()
            last_line += len(lines)

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # Check if this is a noise line.
                matched_noise = next(
                    (p for p in NOISE_PATTERNS if p in line), None
                )
                if matched_noise:
                    noise_counts[matched_noise] = (
                        noise_counts.get(matched_noise, 0) + 1
                    )
                    continue

                # Before emitting a real line, flush any pending noise summary.
                for pattern, count in noise_counts.items():
                    if count:
                        dlog.debug(
                            "[device] ... (%d× '%s')", count, pattern
                        )
                noise_counts.clear()

                dlog.debug("[device] %s", line)

            # At end of each poll, emit noise summary if nothing else fired.
            for pattern, count in noise_counts.items():
                if count:
                    dlog.debug(
                        "[device] ... (%d× '%s')", count, pattern
                    )
            noise_counts.clear()

    def _keep_alive_loop(self) -> None:
        """
        Sends a null keepalive packet to hidg1 every 8 seconds.

        The device firmware increments a counter each second that no data
        arrives on the control HID interface (hidg1).  At count=10 it
        triggers a USB reconnect which kills the ADB input stream.

        We send 1024 null bytes every 8 seconds — enough to reset the
        counter without triggering any command processing (the device
        ignores packets without the ``\x7c\x7c`` header).

        IMPORTANT: Do NOT call disable_small_window_usb() here.
        That command (OUT_SET_SMALL_WINDOW_DATA mode=2) causes
        UlanziDeckKey to reinitialise and then call EVIOCGRAB on
        /dev/input/event0, permanently stealing button events from the
        ADB cat process.  disable_small_window_usb() is called ONCE at
        connect() time instead.

        NOTE: Do NOT run any adb.shell() calls in this loop.  Writing to
        /userdata/keyMode or doing chmod on the manifests triggers inotify
        callbacks in WatcherProcess/UlanziDeckKey, which cause a brief USB
        reconnect that kills /dev/input/event0.
        """
        for tick in itertools.count(1):
            for _ in range(80):   # 8-second interval (80 × 0.1s)
                if not self._running:
                    return
                time.sleep(0.1)
            t0 = time.monotonic()
            log.debug("[keepalive #%d] sending null watchdog reset", tick)
            # TEMP DISABLED: send_hid_keepalive()
            elapsed = time.monotonic() - t0
            log.debug("[keepalive #%d] done in %.3fs", tick, elapsed)

    @staticmethod
    def _print_layout() -> None:
        print()
        print("Ulanzi D200 — controller running. Press Ctrl+C to exit.")
        print()
        print("Button layout (5+5+4; = double-wide LCD):")
        for i, row in enumerate(BUTTON_LAYOUT):
            labels = "  ".join(
                f"[{btn:2d}=]" if btn in WIDE_BUTTONS else
                (f"[{btn:2d}*]" if btn not in BUTTON_TO_MANIFEST_KEY else f"[{btn:2d}]")
                for btn in row
            )
            print(f"  Row {i}:  {labels}")
        print("  (= double-wide LCD)")
        print()
        print("Waiting for button events…")
        print()
