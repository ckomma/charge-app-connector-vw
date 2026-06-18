FROM python:3.11-slim

# Install system dependencies including ADB
RUN apt-get update && apt-get install -y \
    adb \
    usbutils \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY vw_app_connector.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "vw_app_connector.py"]