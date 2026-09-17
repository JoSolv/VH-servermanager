#!/bin/sh
# Prepare /data and, if asked, drop to an unprivileged user before starting.
set -e

DATA="${VHSM_DATA_ROOT:-/data}"
PUID="${PUID:-0}"
PGID="${PGID:-0}"

# steamcmd and the game server both write under HOME. It lives inside the data
# volume so it is writable whichever user we end up running as -- the image's
# default HOME belongs to root.
export HOME="${HOME:-$DATA/home}"
case "$HOME" in
    "$DATA"/*) ;;
    *) HOME="$DATA/home"; export HOME ;;
esac

mkdir -p "$DATA" "$HOME"

if [ "$PUID" = "0" ] && [ "$PGID" = "0" ]; then
    exec "$@"
fi

# A NAS usually wants the files owned by a specific account. Ownership is
# fixed up here rather than in the image, because the uid is only known now.
echo "[entrypoint] running as ${PUID}:${PGID}"
chown -R "${PUID}:${PGID}" "$DATA" 2>/dev/null || \
    echo "[entrypoint] warning: could not chown ${DATA}; check the host permissions"

if ! su -s /bin/sh -c "test -w '$HOME'" "#${PUID}" 2>/dev/null; then
    echo "[entrypoint] warning: ${HOME} is not writable by ${PUID}; steamcmd will fail"
fi

# Capabilities are kept, not dropped: without NET_ADMIN the per-instance
# network counters stop working, and losing them silently would be worse
# than the small privilege they need.
if command -v gosu >/dev/null 2>&1; then
    exec gosu "${PUID}:${PGID}" "$@"
elif command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups "$@"
fi

# Neither tool present: carry on as root rather than refuse to start, but be
# loud about it, since the files will not get the ownership that was asked for.
echo "[entrypoint] ERROR: no gosu or setpriv in the image; continuing as root" >&2
exec "$@"
