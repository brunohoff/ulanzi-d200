"""
CLI entry point — ``python -m ulanzi_d200`` or the ``d200-controller`` script.
"""

import argparse
import json
import logging
import os
import sys

from .controller import D200Controller
from .image_generator import generate_button_images
from .models import D200Config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ulanzi D200 Stream Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Step 1 — generate default numbered button images:
  python -m ulanzi_d200 --generate-images

  # Step 2 — run the controller:
  python -m ulanzi_d200

  # WiFi ADB:
  python -m ulanzi_d200 --device 192.168.1.100:5555

  # Skip icon loading (events only):
  python -m ulanzi_d200 --no-images
""",
    )
    parser.add_argument("--device", metavar="SERIAL", help="ADB device serial or IP:PORT")
    parser.add_argument("--config", "-c", metavar="FILE", default="config.json",
                        help="Path to configuration file (default: config.json)")
    parser.add_argument("--images-dir", "-i", metavar="DIR",
                        help="Directory containing button images")
    parser.add_argument("--state-dir", metavar="DIR",
                        help="Directory to save/load state images")
    parser.add_argument("--mqtt-host", help="MQTT broker host")
    parser.add_argument("--boot-mode", choices=["default", "state"],
                        help="Image source on boot")
    parser.add_argument("--generate-images", action="store_true",
                        help="Generate default numbered button images then exit")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip loading button images (events only)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)8s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load configuration file
    cfg_data: dict = {}
    if os.path.exists(args.config):
        with open(args.config) as f:
            cfg_data = json.load(f)

    # Build config (CLI args take precedence over file)
    config = D200Config()
    if args.images_dir:
        config.images_dir = args.images_dir
    elif "images_dir" in cfg_data:
        config.images_dir = cfg_data["images_dir"]

    if args.state_dir:
        config.state_dir = args.state_dir
    elif "state_dir" in cfg_data:
        config.state_dir = cfg_data["state_dir"]

    if args.mqtt_host:
        config.mqtt_host = args.mqtt_host
    elif "mqtt_host" in cfg_data:
        config.mqtt_host = cfg_data["mqtt_host"]

    for key in ("mqtt_user", "mqtt_pass", "mqtt_send_topic", "mqtt_receive_topic"):
        if key in cfg_data:
            setattr(config, key, cfg_data[key])

    if args.boot_mode:
        config.boot_mode = args.boot_mode
    elif "boot_mode" in cfg_data:
        config.boot_mode = cfg_data["boot_mode"]

    # Generate-images mode
    if args.generate_images:
        generate_button_images(config.images_dir)
        return

    # Controller mode
    ctrl = D200Controller(serial=args.device, config=config)
    if not ctrl.connect():
        sys.exit(1)

    if not args.no_images:
        ctrl.load_all_button_images()

    ctrl.run()


if __name__ == "__main__":
    main()
