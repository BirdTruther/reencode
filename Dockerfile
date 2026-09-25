FROM debian:trixie-slim

# ffmpeg (VAAPI + NVENC), VA drivers for AMD (mesa) and Intel, Python for the dashboard.
RUN set -eux; \
    sed -i 's/^Components: main$/Components: main non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg python3 mesa-va-drivers vainfo tzdata; \
    if [ "$(dpkg --print-architecture)" = amd64 ]; then \
        apt-get install -y --no-install-recommends intel-media-va-driver-non-free i965-va-driver-shaders; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY common.sh reencode.sh encodetv dashboard.py docker-entrypoint.sh /app/
COPY web /app/web

ENV REENCODE_CONFIG=/config/reencode.conf \
    REENCODE_LOG_DIR=/config/logs \
    REENCODE_TEMP_DIR=/transcode \
    HOME=/config \
    PYTHONUNBUFFERED=1 \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

VOLUME ["/config", "/transcode"]
EXPOSE 8686
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python3", "/app/dashboard.py"]
