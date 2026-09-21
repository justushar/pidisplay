FROM debian:trixie-slim

# Debian's prebuilt arm64 packages, not pip: nothing compiles on four A53 cores.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-flask \
        python3-numpy \
        python3-pil \
        ffmpeg \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY display.py index.html test_display.py ./

ENV FB_DEV=/dev/fb1 \
    MEDIA_DIR=/media \
    PORT=8080

EXPOSE 8080
CMD ["python3", "-u", "display.py"]
