#!/bin/sh
# Run as PUID:PGID (the owner of your media) with access to the GPU devices.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
mkdir -p /config /transcode

if [ "$(id -u)" != 0 ]; then
    exec "$@"
fi

getent group "$PGID" >/dev/null || groupadd -g "$PGID" reencode
getent passwd "$PUID" >/dev/null || useradd -u "$PUID" -g "$PGID" -d /config -M -s /usr/sbin/nologin reencode
user=$(getent passwd "$PUID" | cut -d: -f1)

# Join whichever groups own the GPU nodes passed in with --device /dev/dri.
for dev in /dev/dri/renderD* /dev/dri/card*; do
    [ -e "$dev" ] || continue
    gid=$(stat -c %g "$dev")
    [ "$gid" = 0 ] && continue
    getent group "$gid" >/dev/null || groupadd -g "$gid" "gpu$gid"
    usermod -aG "$(getent group "$gid" | cut -d: -f1)" "$user"
done

chown "$PUID:$PGID" /config /transcode 2>/dev/null || true
exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups "$@"
