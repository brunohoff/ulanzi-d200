#!/usr/bin/env python3
"""
MQTT integration test / diagnostic tool for the Ulanzi D200.

Usage:
    # Listen for button events:
    python -m tests.test_mqtt --host localhost

    # Send an image to button 3:
    python -m tests.test_mqtt --host localhost --button 3 --image path/to/image.png
"""

import argparse
import base64
import json
import os
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Please install paho-mqtt: pip install paho-mqtt")
    raise SystemExit(1)


def on_connect(client, userdata, flags, rc):
    print(f"Connected to MQTT broker with result code {rc}")
    client.subscribe(userdata['receive_topic'])
    print(f"Subscribed to {userdata['receive_topic']} to listen for button events.")


def on_message(client, userdata, msg):
    print(f"\n[RECEIVED EVENT] Topic: {msg.topic}")
    try:
        payload = json.loads(msg.payload.decode())
        print(f"  Button: {payload.get('button')}")
        print(f"  Action: {payload.get('action')}")
    except json.JSONDecodeError:
        print(f"  Raw payload: {msg.payload.decode()}")


def send_image(client, topic: str, button: int, image_path: str) -> None:
    if not os.path.exists(image_path):
        print(f"Error: Image {image_path} not found.")
        return

    with open(image_path, "rb") as f:
        img_data = f.read()

    b64_img = base64.b64encode(img_data).decode('utf-8')
    payload = {"button": button, "image": b64_img}

    print(f"Sending image {image_path} to button {button} via topic {topic}...")
    client.publish(topic, json.dumps(payload))
    print("Sent.")


def main():
    parser = argparse.ArgumentParser(
        description="MQTT integration test for the Ulanzi D200"
    )
    parser.add_argument("--host", default="localhost", help="MQTT broker host")
    parser.add_argument("--user", default="", help="MQTT username")
    parser.add_argument("--passw", default="", help="MQTT password")
    parser.add_argument("--send-topic", default="ulanzi/receive",
                        help="Topic to send images to (application's receive topic)")
    parser.add_argument("--receive-topic", default="ulanzi/send",
                        help="Topic to receive events from (application's send topic)")
    parser.add_argument("--button", type=int, help="Button number to update")
    parser.add_argument("--image", help="Path to image to send to the button")

    args = parser.parse_args()

    client = mqtt.Client(userdata={"receive_topic": args.receive_topic})

    if args.user:
        client.username_pw_set(args.user, args.passw)

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.host)
    except Exception as e:
        print(f"Failed to connect to MQTT broker at {args.host}: {e}")
        return

    client.loop_start()

    if args.button and args.image:
        time.sleep(1)
        send_image(client, args.send_topic, args.button, args.image)

    print("\nListening for button events. Press Ctrl+C to exit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
