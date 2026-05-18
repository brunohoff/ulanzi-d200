"""
ADB client wrapper for communicating with the Ulanzi D200 over USB.
"""

import logging
import subprocess
from typing import List, Optional

log = logging.getLogger(__name__)


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
        """Open a persistent process (e.g., for streaming input events)."""
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
        model = self.shell("getprop ro.product.model 2>/dev/null")
        if not model or "not found" in model:
            model = self.shell("uname -n 2>/dev/null") or "Ulanzi D200"
        return model
