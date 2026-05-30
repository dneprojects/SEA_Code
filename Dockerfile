# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
# Base image (multi-arch) + labels live here now; build.yaml is no longer used.
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

# Alpine-based HA base image -> install Python via apk.
RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# Application code
COPY smart_energy_agent /app/smart_energy_agent
COPY web /app/web
COPY run.sh /app/run.sh
RUN chmod a+x /app/run.sh

LABEL \
    org.opencontainers.image.title="Smart Energy Agent" \
    org.opencontainers.image.description="Energy-aware monitoring and PV-surplus control for Home Assistant" \
    org.opencontainers.image.source="https://github.com/dneprojects/SAE_Code" \
    org.opencontainers.image.licenses="MIT"

CMD [ "/app/run.sh" ]
