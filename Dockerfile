FROM python:3.11-slim

# Install ADB
RUN apt-get update && apt-get install -y --no-install-recommends \
    adb \
    usbutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies and the package in editable mode
COPY requirements.txt pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e .

# Copy remaining project files
COPY . .

# Ensure runtime directories exist
RUN mkdir -p /app/state_images /app/button_images

# Run the controller via the installed entry point
ENTRYPOINT ["python", "-m", "ulanzi_d200"]
