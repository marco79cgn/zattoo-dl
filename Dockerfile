FROM alpine:3.22.0

RUN apk add --no-cache bash curl yt-dlp jq util-linux

SHELL ["/bin/bash", "-c"]

WORKDIR /data

COPY zattoo-dl.sh /usr/local/bin/zattoo-dl.sh

RUN chmod +x /usr/local/bin/zattoo-dl.sh

ENTRYPOINT ["/usr/local/bin/zattoo-dl.sh"]