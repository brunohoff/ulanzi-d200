# Ulanzi D200 Stream Controller

This project provides a Python controller for the Ulanzi D200 hardware, enabling integration with an MQTT broker. You can track button presses and dynamically update button images using base64 encoded images.

## Features

- Read physical button presses and detect single, double, and long presses.
- Send button events over MQTT.
- Receive base64 images over MQTT to update button icons dynamically.
- Persistent state management to survive reboots (`boot_mode`).
- Run natively or via Docker.

## Setup

### Native Requirements
1. Python 3.11+
2. Install `adb` (`sudo apt install adb` on Ubuntu/Debian)
3. Install dependencies: `pip install -r requirements.txt`

### Using Docker
You can run the controller using the provided `docker-compose.yml`.
```bash
docker compose up -d
```
Note: Ensure your device is connected via USB and `adb` debugging is enabled. The container mounts `/dev/bus/usb` with `privileged: true` to access the gadget.

## Configuration

Configuration can be provided via `config.json` or command-line arguments (which override the file).

First, create your configuration file from the template:
```bash
cp config.example.json config.json
```

**config.json**
```json
{
  "mqtt_host": "localhost",
  "mqtt_user": "",
  "mqtt_pass": "",
  "mqtt_send_topic": "ulanzi/send",
  "mqtt_receive_topic": "ulanzi/receive",
  "boot_mode": "default"
}
```

- `boot_mode`: Can be `"default"` or `"state"`.
  - `"default"`: Always boots using the images in the `button_images` directory.
  - `"state"`: Boots using images saved in `state_images` (which are updated via MQTT). If an image doesn't exist in `state_images`, it falls back to `button_images`.

## MQTT Payload Format

### Publishing (Events)
When a button is pressed, the controller publishes a JSON payload to `mqtt_send_topic`:
```json
{
  "button": 1,
  "action": "single_press"
}
```
Possible actions: `down`, `up`, `single_press`, `double_press`, `long_press`.

### Subscribing (Image Updates)
To update a button's image, publish a JSON payload to `mqtt_receive_topic`:
```json
{
  "button": 1,
  "image": "iVBORw0KGgoAAAANSUhEUgAA..."
}
```
The `image` field must be a valid base64-encoded PNG string. The controller will decode, resize appropriately (196x196 or 392x196 for button 14), and push it to the device.

## Commands

**Generate Default Images**
```bash
python3 d200_controller.py --generate-images
```

**Run Controller**
```bash
python3 d200_controller.py --config config.json
```

**View All Options**
```bash
python3 d200_controller.py --help
```
