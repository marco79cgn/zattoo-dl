FROM alpine:3.22.0

RUN apk add --no-cache bash curl python3 yt-dlp ffmpeg jq util-linux

SHELL ["/bin/bash", "-c"]

WORKDIR /data

# Bash-CLI
COPY zattoo-dl.sh /usr/local/bin/zattoo-dl.sh
RUN chmod +x /usr/local/bin/zattoo-dl.sh

# Web-GUI (Python-Stdlib-Server + statisches Frontend)
COPY zattoo-gui.py /opt/zattoo-dl/zattoo-gui.py
COPY gui /opt/zattoo-dl/gui

# Default: das Bash-CLI (rückwärtskompatibel zu bisherigen Aufrufen).
# Für die GUI: docker run ... ghcr.io/marco79cgn/zattoo-dl python3 /opt/zattoo-dl/zattoo-gui.py --bind 0.0.0.0 --no-browser
ENTRYPOINT ["/usr/local/bin/zattoo-dl.sh"]
