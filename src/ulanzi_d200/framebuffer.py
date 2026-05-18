"""
LCD framebuffer manager — pushes icons to the Ulanzi D200 via ADB.
"""

import json
import logging
import os
import struct
import sys
import tempfile
import time
from datetime import datetime
from typing import Dict, Optional

from PIL import Image

from .adb_client import ADBClient
from .constants import (
    ALL_DEVICE_MANIFESTS,
    BUTTON_TO_MANIFEST_KEY,
    DEVICE_IMAGES_DIR,
    DEVICE_MANIFEST,
    KEY_MODE_FILE,
    LCD_H,
    LCD_W,
    WIDE_BUTTONS,
    WIDE_LCD_W,
)

log = logging.getLogger(__name__)

_TARGET_VID = 0x2207
_TARGET_PID = 0x0019
_small_window_warned = False


def _build_small_window_packet() -> bytes:
    """Build the USB HID packet that sets Small Window to BACKGROUND mode (mode=2).

    Packet format matches strmdck's PacketStruct:
      \x7c\x7c  — 2-byte header
      command   — 0x0006 big-endian  (OUT_SET_SMALL_WINDOW_DATA)
      length    — 4-byte little-endian of data length
      data      — padded to 1016 bytes

    Data string: "mode|cpu|mem|HH:MM:SS|gpu"
      mode=2 → BACKGROUND (shows our button image, hides the Ulanzi overlay)
    """
    now = datetime.now().strftime("%H:%M:%S")
    data_str = f"2|0|0|{now}|0".encode()
    header = b"\x7c\x7c"
    cmd = struct.pack(">H", 0x0006)       # OUT_SET_SMALL_WINDOW_DATA
    length = struct.pack("<I", len(data_str))
    padded_data = data_str.ljust(1016, b"\x00")
    return header + cmd + length + padded_data


def _send_hid_win32(packet: bytes) -> bool:
    """
    Send a HID output report on Windows using ctypes only — no external
    packages required.  Returns True on success.

    Targets the *control* HID interface (hid.usb1 / /dev/hidg1, 1024-byte
    output reports) — NOT the keyboard interface (hid.usb0 / /dev/hidg0,
    8-byte reports).  We identify the correct interface by checking
    OutputReportByteLength via HidP_GetCaps.
    """
    import ctypes
    import ctypes.wintypes as wt

    try:
        setupapi = ctypes.WinDLL("setupapi")
        kernel32 = ctypes.WinDLL("kernel32")
        hid_dll  = ctypes.WinDLL("hid")

        # ── CRITICAL: set restype for functions that return HANDLE / HDEVINFO.
        # Without this ctypes defaults to c_int (32-bit) and truncates the
        # pointer on 64-bit Windows, making every subsequent call invalid.
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        setupapi.SetupDiGetClassDevsW.restype          = ctypes.c_void_p
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        kernel32.CreateFileW.restype                   = ctypes.c_void_p
        kernel32.CloseHandle.argtypes                  = [ctypes.c_void_p]
        kernel32.CloseHandle.restype                   = wt.BOOL
        kernel32.WriteFile.argtypes                    = [
            ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
            ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
        ]
        kernel32.WriteFile.restype = wt.BOOL
        hid_dll.HidD_GetAttributes.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        hid_dll.HidD_GetAttributes.restype = wt.BOOL
        hid_dll.HidD_GetPreparsedData.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        hid_dll.HidD_GetPreparsedData.restype = wt.BOOL
        hid_dll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
        hid_dll.HidD_FreePreparsedData.restype = wt.BOOL
        hid_dll.HidP_GetCaps.restype = ctypes.c_long  # NTSTATUS
        hid_dll.HidD_SetOutputReport.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wt.ULONG,
        ]
        hid_dll.HidD_SetOutputReport.restype = wt.BOOL

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                        ("Data3", wt.WORD),  ("Data4", ctypes.c_uint8 * 8)]

        class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                        ("Flags", wt.DWORD),  ("Reserved", ctypes.c_uint64)]

        class HIDD_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Size", wt.ULONG),    ("VendorID",  wt.USHORT),
                        ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]

        # First 5 fields of HIDP_CAPS are enough for our purpose.
        class HIDP_CAPS(ctypes.Structure):
            _fields_ = [
                ("Usage",                   wt.USHORT),
                ("UsagePage",               wt.USHORT),
                ("InputReportByteLength",   wt.USHORT),
                ("OutputReportByteLength",  wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT),
                ("Reserved",               wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps",   wt.USHORT),
                ("NumberInputValueCaps",    wt.USHORT),
                ("NumberInputDataIndices",  wt.USHORT),
                ("NumberOutputButtonCaps",  wt.USHORT),
                ("NumberOutputValueCaps",   wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT),
                ("NumberFeatureValueCaps",  wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT),
            ]

        guid = GUID()
        hid_dll.HidD_GetHidGuid(ctypes.byref(guid))

        DIGCF_PRESENT          = 0x02
        DIGCF_DEVICEINTERFACE  = 0x10
        hdev_int = setupapi.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
        )
        if not hdev_int or hdev_int == INVALID_HANDLE_VALUE:
            log.debug("SetupDiGetClassDevsW failed (no HID devices?)")
            return False
        # Wrap raw int as c_void_p so ctypes can pass it back to API functions
        # without OverflowError (plain ints > 2^31 overflow the default c_int).
        hdev = ctypes.c_void_p(hdev_int)

        try:
            idx = 0
            while True:
                iface = SP_DEVICE_INTERFACE_DATA()
                iface.cbSize = ctypes.sizeof(iface)
                if not setupapi.SetupDiEnumDeviceInterfaces(
                    hdev, None, ctypes.byref(guid), idx, ctypes.byref(iface)
                ):
                    break
                idx += 1

                req = wt.DWORD(0)
                setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(iface), None, 0, ctypes.byref(req), None
                )
                if not req.value:
                    continue

                buf = ctypes.create_string_buffer(req.value)
                # cbSize of SP_DEVICE_INTERFACE_DETAIL_DATA_W:
                #   8 bytes on 64-bit Windows, 6 bytes on 32-bit.
                ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = (
                    8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                )
                if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(iface), buf, req, None, None
                ):
                    continue

                # Device path starts right after the 4-byte cbSize field.
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)

                # Open with read+write so we can check capabilities AND write.
                # The keyboard interface (GENERIC_WRITE only needed) is usually
                # exclusively held by the Windows HID class driver; that's fine
                # — we only want the control interface anyway.
                GENERIC_READ_WRITE = 0xC0000000
                FILE_SHARE_RW      = 0x00000003
                OPEN_EXISTING      = 3

                handle_int = kernel32.CreateFileW(
                    path, GENERIC_READ_WRITE, FILE_SHARE_RW,
                    None, OPEN_EXISTING, 0, None
                )
                if handle_int is None or handle_int == INVALID_HANDLE_VALUE:
                    continue
                handle = ctypes.c_void_p(handle_int)  # wrap for safe passing

                try:
                    attrs = HIDD_ATTRIBUTES()
                    attrs.Size = ctypes.sizeof(attrs)
                    if not hid_dll.HidD_GetAttributes(handle, ctypes.byref(attrs)):
                        continue
                    if attrs.VendorID != _TARGET_VID or attrs.ProductID != _TARGET_PID:
                        continue

                    # Identify the control interface by checking OutputReportByteLength.
                    # hid.usb1 (control) has 1024-byte reports → Windows reports 1025
                    # (includes the implicit report-ID byte).
                    # hid.usb0 (keyboard) has 8-byte reports → Windows reports 9.
                    preparsed = ctypes.c_void_p(None)
                    if not hid_dll.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
                        log.debug("HID path %s: GetPreparsedData failed", path[-50:])
                        continue
                    try:
                        caps = HIDP_CAPS()
                        HIDP_STATUS_SUCCESS = 0x00110000
                        if hid_dll.HidP_GetCaps(preparsed, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
                            continue
                        if caps.OutputReportByteLength < 1025:
                            log.debug(
                                "HID path %s: OutputReport=%d — skip (keyboard interface)",
                                path[-50:], caps.OutputReportByteLength,
                            )
                            continue
                        log.debug(
                            "HID control interface: %s (OutputReport=%d)",
                            path[-60:], caps.OutputReportByteLength,
                        )
                    finally:
                        hid_dll.HidD_FreePreparsedData(preparsed)

                    # Send via HidD_SetOutputReport (most reliable), fall back to WriteFile.
                    report = (b"\x00" + packet).ljust(caps.OutputReportByteLength, b"\x00")
                    if hid_dll.HidD_SetOutputReport(handle, report, len(report)):
                        log.debug("Control HID write: %d bytes (HidD_SetOutputReport).", len(report))
                        return True
                    written = wt.DWORD(0)
                    if kernel32.WriteFile(
                        handle, report, len(report), ctypes.byref(written), None
                    ):
                        log.debug("Control HID write: %d bytes (WriteFile).", written.value)
                        return True
                    err = ctypes.windll.kernel32.GetLastError()
                    log.warning(
                        "HID write failed on control interface (Win32 error %d)", err,
                    )
                finally:
                    kernel32.CloseHandle(handle)
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(hdev)
    except Exception as e:
        log.warning("Windows HID ctypes fallback error: %s", e)
    return False


def _send_to_control_hid(packet: bytes) -> bool:
    """
    Send *packet* (exactly 1024 bytes) to the D200 control HID interface.

    Tries (in order):
      1. Python ``hid`` package  (cross-platform)
      2. Windows-native ctypes   (Windows only, no extra packages)

    Returns True if the data was written to the device, False otherwise.
    """
    # ── Method 1: hid package ────────────────────────────────────────────────
    try:
        import hid  # type: ignore[import]

        # The D200 is a USB composite device with two HID interfaces:
        #   hid.usb0 / /dev/hidg0  — keyboard (8-byte reports, usage page 0x0001)
        #   hid.usb1 / /dev/hidg1  — control  (1024-byte reports, usage page 0x000C)
        # We must target the CONTROL interface; hid.open(VID,PID) opens the
        # first matching interface which is typically the keyboard — wrong.
        # Use hid.enumerate() to find the control interface by usage_page.
        matches = [
            d for d in hid.enumerate(_TARGET_VID, _TARGET_PID)
            if d.get("usage_page") == 0x000C  # Consumer Devices = control iface
        ]
        if not matches:
            log.debug(
                "hid.enumerate: no control interface found for %04x:%04x "
                "(usage_page=0x000C) — falling through to ctypes",
                _TARGET_VID, _TARGET_PID,
            )
            raise ImportError  # fall through to Method 2
        ctrl_path = matches[0]["path"]

        dev = hid.device()
        try:
            dev.open_path(ctrl_path)
        except Exception as open_err:
            log.warning(
                "HID open_path failed: %s — falling through to ctypes", open_err,
            )
            raise ImportError  # fall through to Method 2
        dev.set_nonblocking(True)
        try:
            # Prepend report-ID byte (0x00 = no named report IDs).
            n = dev.write(b"\x00" + packet)
            if n < 0:
                log.warning("HID write returned %d — may not have been sent", n)
                return False
            log.debug("Control HID write: %d bytes (hid package).", n)
            return True
        except Exception as write_err:
            log.warning("HID write failed: %s", write_err)
            return False
        finally:
            dev.close()
    except ImportError:
        pass
    except Exception as e:
        log.warning("hid package error: %s", e)

    # ── Method 2: Windows ctypes ─────────────────────────────────────────────
    if sys.platform == "win32":
        return _send_hid_win32(packet)

    return False


def disable_small_window_usb() -> None:
    """
    Disable the Ulanzi Small Window overlay on Button 14.

    Sends ``OUT_SET_SMALL_WINDOW_DATA`` (mode=2 / BACKGROUND) once.
    Call this once at startup; do **not** call it repeatedly — sending it
    every second causes UlanziDeckKey to reinitialise and grab /dev/input/event0
    exclusively, which permanently blocks the ADB button-event stream.

    For periodic watchdog resets use :func:`send_hid_keepalive` instead.
    """
    global _small_window_warned
    packet = _build_small_window_packet()
    if _send_to_control_hid(packet):
        log.info("Small Window disabled via HID (mode=2).")
        return

    if not _small_window_warned:
        log.warning(
            "Cannot disable Small Window overlay on Button 14. "
            "Install the 'hid' package to fix this: pip install hid"
        )
        _small_window_warned = True


def send_hid_keepalive() -> bool:
    """
    Reset the device watchdog by sending a harmless null packet.

    The D200 firmware counts seconds of inactivity on the control HID
    interface (hidg1).  At count=10 it triggers a USB reconnect that kills
    the ADB input stream.  Sending *any* data to hidg1 resets the counter.

    This function sends 1024 null bytes — no ``||`` header, so the device's
    command parser ignores the content and no side-effects occur.

    Call this every ~8 seconds (the watchdog fires at 10 s).
    """
    packet = b"\x00" * 1024
    return _send_to_control_hid(packet)


class D200Framebuffer:
    """
    Manages the LCD button displays via ADB + manifest.json.

    The UlanziDeckKey Qt app owns /dev/fb0 and renders button icons based on
    /tmp/standalone/manifest.json.  This class:
      1. Resizes icons to 196×196 PNG and pushes them to /tmp/standalone/Images/
      2. Rewrites manifest.json with the new icon paths
      3. Kills UlanziDeckKey — the init script restarts it and it reads the
         updated manifest on boot (~3-second reload time)

    Call set_button_image() for each button, then apply() once to commit.
    """

    def __init__(self, adb: ADBClient):
        self._adb = adb
        self._pending: Dict[int, str] = {}          # button → local PNG path
        self._manifest: Dict[str, dict] = {}        # COL_ROW → manifest entry
        self._dummy_mode = False

    # ── Device probing ────────────────────────────────────────────────────────

    def probe(self) -> bool:
        """
        Verify the device manifest is accessible and load its current contents.
        Returns True if the manifest was found and loaded.
        """
        key_mode = self._adb.shell(f"cat {KEY_MODE_FILE} 2>/dev/null").strip()
        log.info("Device keyMode on connect: '%s'", key_mode)
        if key_mode != "win":
            log.info("Setting keyMode to 'win'.")
            self._adb.shell(f"printf 'win' > {KEY_MODE_FILE} 2>/dev/null")

        # Log /tmp/standalone/ so we can see all config files on the device.
        ls_out = self._adb.shell("ls -la /tmp/standalone/ 2>/dev/null")
        log.info("Device /tmp/standalone/:\n%s", ls_out)

        log.info("Checking manifest on device: %s", DEVICE_MANIFEST)

        raw = self._adb.shell(f"cat {DEVICE_MANIFEST} 2>/dev/null")
        if not raw:
            log.warning(
                "Manifest not found at %s — will create it on apply().",
                DEVICE_MANIFEST,
            )
            self._manifest = {}
            return True

        try:
            self._manifest = json.loads(raw)
            log.info("Loaded manifest with %d entries", len(self._manifest))
            # Dump the full manifest so we can inspect button 14's fields
            # (keyboard action, key codes, etc.) to understand what to clear.
            log.info("Original manifest (button 14 entries 3_2 / 4_2):\n  3_2: %s\n  4_2: %s",
                     json.dumps(self._manifest.get("3_2")),
                     json.dumps(self._manifest.get("4_2")))
            log.debug("Full original manifest:\n%s",
                      json.dumps(self._manifest, indent=2))
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

            entry = {
                "State": 0,
                "ViewParam": [{"Icon": f"Images/{device_img_name}"}],
            }
            self._manifest[manifest_key] = entry
            # Button 14 is double-wide (cols 3–4 in row 2).  The device may
            # use "4_2" as the display key for the full double-wide LCD panel
            # *or* for the "Small Window" overlay on the right half.  Writing
            # our image to both keys ensures it shows regardless.
            if btn in WIDE_BUTTONS:
                self._manifest["4_2"] = entry
                log.debug("Button %d → also wrote '4_2' manifest key", btn)
            log.info("Button %d → %s", btn, device_img_name)

        # Remove stale manifest entries
        valid_keys = set(BUTTON_TO_MANIFEST_KEY.values())
        valid_keys.add("4_2")  # keep even if not a physical button (double-wide)
        stale = [k for k in self._manifest if k not in valid_keys]
        for k in stale:
            del self._manifest[k]
            log.debug("Removed stale manifest entry: %s", k)

        # Write manifest via temp file
        manifest_json = json.dumps(self._manifest, indent=2)
        local_manifest = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as tmp:
                tmp.write(manifest_json)
                local_manifest = tmp.name

            # Make manifest files writable before pushing (a previous apply()
            # may have left them read-only).
            self._adb.shell(
                "chmod 666 /tmp/standalone/manifest.json"
                " /tmp/standalone/manifest1.json"
                " /tmp/standalone/manifest2.json 2>/dev/null"
            )

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

            # Lock manifests read-only so WatcherProcess cannot overwrite them.
            self._adb.shell(
                "chmod 444 /tmp/standalone/manifest.json"
                " /tmp/standalone/manifest1.json"
                " /tmp/standalone/manifest2.json 2>/dev/null"
            )
            log.info("Manifests locked read-only")
        finally:
            if local_manifest:
                try:
                    os.unlink(local_manifest)
                except OSError:
                    pass

        # Disable the Small Window overlay on Button 14 before restarting
        # UlanziDeckKey.  Sending it here (rather than in connect()) ensures
        # the mode-2 command arrives while UlanziDeckKey is alive to process
        # it, and the subsequent kill + restart cycle below absorbs any
        # self-restart the command might trigger — so the input listener is
        # never started against a mid-restart device.
        disable_small_window_usb()

        log.info("Restarting UlanziDeckKey to apply icon changes (~6 s)...")
        print("[INFO]  Restarting device app to apply icons (~6 s)...")
        # Strategy: set keyMode FIRST, then kill only UlanziDeckKey so that
        # WatcherProcess (which IS the watchdog for UlanziDeckKey) restarts it
        # naturally using our keyMode value and our locked manifest files.
        # Do NOT kill WatcherProcess here — that causes init to restart it,
        # which can reset the profile before UlanziDeckKey has even started.
        self._adb.shell(
            f"printf 'win' > {KEY_MODE_FILE} 2>/dev/null;"
            f" kill $(pidof UlanziDeckKey) 2>/dev/null"
        )
        time.sleep(6.0)  # give WatcherProcess time to restart UlanziDeckKey

        pid = self._adb.shell("pidof UlanziDeckKey 2>/dev/null").strip()
        if not pid:
            # WatcherProcess did not restart UlanziDeckKey — start it ourselves.
            log.warning("UlanziDeckKey not restarted by WatcherProcess — starting manually.")
            self._adb.shell(
                f"printf 'win' > {KEY_MODE_FILE} 2>/dev/null;"
                f" setsid /userdata/UlanziDeckKey -platform linuxfb >/dev/null 2>&1 &"
            )
            time.sleep(4.0)
            pid = self._adb.shell("pidof UlanziDeckKey 2>/dev/null").strip()
            if not pid:
                log.error("UlanziDeckKey failed to start")
                success = False

        if pid:
            log.info("UlanziDeckKey running (PID %s)", pid)
            disable_small_window_usb()

        self._pending.clear()
        return success
