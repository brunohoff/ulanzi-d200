FROM python:3.11-slim

# Install ADB
RUN apt-get update && apt-get install -y --no-install-recommends \
    adb \
    usbutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure the state directory exists
RUN mkdir -p /app/state_images /app/button_images

# Run the controller
ENTRYPOINT ["python", "d200_controller.py"]
