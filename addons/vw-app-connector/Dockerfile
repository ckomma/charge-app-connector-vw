ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    android-tools \
    ca-certificates \
    curl \
    jq \
    procps \
    py3-pip \
    python3 \
    usbutils

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY vw_app_connector.py mqtt_publisher.py /app/
COPY run.sh /run.sh

RUN chmod a+x /run.sh

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import json, os, urllib.request; port=os.getenv('PORT', '9920'); json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/capabilities', timeout=3))"

CMD ["/run.sh"]
