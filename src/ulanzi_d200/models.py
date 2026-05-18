"""
Data models for the Ulanzi D200 controller.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional


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
