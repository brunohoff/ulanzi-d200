"""
Raw input event reader and press-type detector for the Ulanzi D200.
"""

import logging
import queue
import struct
import subprocess
import threading
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional

from .adb_client import ADBClient
from .constants import (
    DOUBLE_PRESS_SEC,
    INPUT_DEVICE,
    INPUT_EVENT_FMT,
    INPUT_EVENT_SIZE,
    KEY_TO_BUTTON,
    LONG_PRESS_SEC,
)
from .models import ButtonState

log = logging.getLogger(__name__)


class D200Input:
    """
    Reads raw binary input events from /dev/input/event0 via `adb shell cat`.

    The device uses a matrix-keypad driver.  struct input_event on 32-bit ARM
    is 16 bytes:  tv_sec(4) tv_usec(4) type(2) code(2) value(4)

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
        for state in self._states.values():
            state.cancel_timers()
        log.info("Input listener stopped")

    # ── Event loop ────────────────────────────────────────────────────────────

    # How long to wait without any input event before treating the stream as
    # dead.  UlanziDeckKey can call EVIOCGRAB on /dev/input/event0 which
    # silently steals events from our ADB cat — the process stays alive but
    # nothing arrives.  The timeout lets us detect and reconnect.
    _READ_TIMEOUT_S = 30.0

    def _read_loop(self):
        """
        Read binary input_event structs from /dev/input/event0 via adb shell cat.
        Each struct is INPUT_EVENT_SIZE (16) bytes on 32-bit ARM.

        Automatically reconnects if the adb process dies (e.g., after
        UlanziDeckKey is restarted by apply()), or if no events arrive for
        _READ_TIMEOUT_S seconds (e.g., due to EVIOCGRAB by UlanziDeckKey).
        """
        while self._running:
            try:
                self._proc = self._adb.popen(
                    "shell",
                    f"stty -onlcr; cat {INPUT_DEVICE}",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                # Feed raw bytes into a queue from a dedicated reader thread so
                # we can apply a timeout.  On Windows select() doesn't work on
                # subprocess pipes.
                chunk_q: queue.Queue = queue.Queue()

                def _pipe_reader():
                    try:
                        while True:
                            data = self._proc.stdout.read(INPUT_EVENT_SIZE)
                            chunk_q.put(data)
                            if not data:
                                break
                    except Exception:
                        chunk_q.put(b"")

                reader = threading.Thread(target=_pipe_reader, daemon=True,
                                          name="d200-input-reader")
                reader.start()

                buf = b""
                while self._running:
                    try:
                        chunk = chunk_q.get(timeout=self._READ_TIMEOUT_S)
                    except queue.Empty:
                        log.debug(
                            "Input stream: no events for %.0fs — assuming stream dead",
                            self._READ_TIMEOUT_S,
                        )
                        break
                    if not chunk:
                        log.debug("Input stream: empty read — stream died")
                        break
                    buf += chunk
                    while len(buf) >= INPUT_EVENT_SIZE:
                        event_data = buf[:INPUT_EVENT_SIZE]
                        buf = buf[INPUT_EVENT_SIZE:]
                        self._parse_event(event_data)
            except Exception as e:
                if self._running:
                    log.error("Input read loop error: %s", e)
            finally:
                if self._proc is not None:
                    try:
                        self._proc.terminate()
                    except Exception:
                        pass
                    self._proc = None

            if self._running:
                t_died = time.monotonic()
                log.info("Input stream died at t=%.3f — waiting for ADB to reconnect…",
                         t_died)
                # Give the device time to complete its USB reconnect cycle,
                # then wait until ADB is back online (up to 30 s).
                time.sleep(2.0)
                for attempt in range(30):
                    if not self._running:
                        break
                    if self._adb.is_connected():
                        log.info("ADB reconnected after %.1fs (attempt %d)",
                                 time.monotonic() - t_died, attempt + 1)
                        break
                    log.debug("Waiting for ADB... attempt %d", attempt + 1)
                    time.sleep(1.0)
                if self._running:
                    log.info("Restarting input listener")

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
        state.cancel_timers()

        if self.on_press:
            self.on_press(button)

        timer = threading.Timer(LONG_PRESS_SEC, self._fire_long_press, args=(button,))
        timer.daemon = True
        timer.start()
        state.long_timer = timer

    def _on_key_up(self, button: int, t: float):
        state = self._states[button]
        hold = t - state.press_time

        if state.long_timer:
            state.long_timer.cancel()
            state.long_timer = None

        if self.on_release:
            self.on_release(button)

        if state.long_press_fired:
            state.long_press_fired = False
            return

        if hold >= LONG_PRESS_SEC:
            return

        state.press_count += 1

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
        state.press_count = 0
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
