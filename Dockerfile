# Valheim Server Manager
#
# amd64 only, deliberately: the Valheim dedicated server is an x86_64 binary,
# so an arm64 image would build and then fail to run anything useful.
FROM python:3.11-slim-bookworm

# steamcmd itself is a 32-bit binary, hence the i386 architecture and
# lib32gcc-s1; the rest are what the 64-bit Valheim server links against.
# nftables is optional and only used for per-instance network counters.
# gosu drops privileges cleanly when PUID/PGID are set (setpriv lives in
# util-linux-extra on bookworm, not util-linux, so it is not a safe default).
# en_US.UTF-8 is generated because steamcmd asks for it by name, and without
# a UTF-8 locale non-ASCII server, world and player names get mangled.
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      lib32gcc-s1 \
      libatomic1 \
      libpulse0 \
      libstdc++6 \
      zlib1g \
      nftables \
      tzdata \
      gosu \
      locales \
 && sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen \
 && locale-gen \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY vhsm ./vhsm
COPY tools ./tools
COPY run.py ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV VHSM_DATA_ROOT=/data \
    VHSM_HOST=0.0.0.0 \
    VHSM_PORT=8080 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# steamcmd keeps its state under $HOME/Steam. The image's default HOME is not
# writable by an unprivileged PUID, so it is pointed inside the data volume.
ENV HOME=/data/home

# Server files, worlds, mods and backups all live here. Mount it, or an
# update is a fresh 2 GB download every time the container is recreated.
VOLUME ["/data"]

EXPOSE 8080/tcp
# One instance uses three consecutive UDP ports starting at its game port.
EXPOSE 2456-2458/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('VHSM_PORT','8080')+'/api/host',timeout=4)" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "run.py"]
