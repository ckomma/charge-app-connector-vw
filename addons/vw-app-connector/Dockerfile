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

ARG ADDON_RAW_URL=https://raw.githubusercontent.com/ckomma/charge-app-connector-vw/9454854/addons/vw-app-connector
ARG ADDON_VERSION=0.1.8

RUN curl -fsSL "$ADDON_RAW_URL/requirements.txt?v=$ADDON_VERSION" -o requirements.txt \
    && pip3 install --break-system-packages --no-cache-dir -r requirements.txt

RUN curl -fsSL "$ADDON_RAW_URL/vw_app_connector.py?v=$ADDON_VERSION" -o vw_app_connector.py \
    && curl -fsSL "$ADDON_RAW_URL/mqtt_publisher.py?v=$ADDON_VERSION" -o mqtt_publisher.py \
    && curl -fsSL "$ADDON_RAW_URL/run.sh?v=$ADDON_VERSION" -o /run.sh \
    && chmod a+x /run.sh

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import json, os, urllib.request; port=os.getenv('PORT', '9920'); json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/capabilities', timeout=3))"

CMD ["/run.sh"]
