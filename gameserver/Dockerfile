FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive

# WineHQ stable is considerably newer than Debian's packaged wine, which matters
# for .NET executables like BannerlordCoopServer.exe.
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        gnupg \
        xvfb \
        xauth \
        cabextract \
    && mkdir -p /etc/apt/keyrings \
    && wget -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key \
    && wget -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/debian/dists/bookworm/winehq-bookworm.sources \
    && apt-get update \
    && apt-get install -y --install-recommends winehq-stable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Keep the wine prefix on the shared volume so it survives image rebuilds
ENV WINEPREFIX=/data/wineprefix \
    WINEDEBUG=-all

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /data/server

ENTRYPOINT ["/entrypoint.sh"]
