FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        adb \
        ca-certificates \
        procps \
        usbutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY vw_app_connector.py mqtt_publisher.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 9920

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import json, os, urllib.request; port=os.getenv('PORT', '9920'); json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3))"

CMD ["python", "vw_app_connector.py"]
