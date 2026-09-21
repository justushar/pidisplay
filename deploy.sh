#!/usr/bin/env bash
# Deploy pidisplay to the Raspberry Pi. Run from this directory.
#
#   ./deploy.sh                          # SSH key auth (recommended)
#   PI_PASS=... ./deploy.sh              # password auth
#   PI_HOST=user@host ./deploy.sh        # different target
#
# Credentials are never stored here. Put local settings in .deploy.env
# (gitignored) if you want them remembered:
#   PI_HOST=pi@192.168.1.50
#   PI_PASS=secret
#
# Requires podman on the Pi, once:
#   ssh <host> 'sudo apt update && sudo apt install -y podman'
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=/dev/null
[ -f .deploy.env ] && . ./.deploy.env

PI_HOST="${PI_HOST:-pi@raspberrypi.local}"
PI_PASS="${PI_PASS:-}"

OPTS=(-o StrictHostKeyChecking=accept-new)

if [ -n "$PI_PASS" ]; then
    # Feed ssh the password without needing a tty. The helper reads it from the
    # environment rather than having it baked in, so it never touches disk.
    ASKPASS="$(mktemp)"
    printf '#!/bin/sh\nprintf "%%s" "$PI_PASS"\n' > "$ASKPASS"
    chmod 700 "$ASKPASS"
    trap 'rm -f "$ASKPASS"' EXIT
    export PI_PASS
    export SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY=:0
    OPTS+=(-o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1)
fi

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

say "copying source to $PI_HOST:~/pidisplay/src"
ssh "${OPTS[@]}" "$PI_HOST" 'mkdir -p ~/pidisplay/src ~/pidisplay/media'
scp "${OPTS[@]}" -q \
    display.py index.html test_display.py Containerfile pidisplay.container \
    "$PI_HOST:pidisplay/src/"

say "building image, running checks, restarting service"
ssh "${OPTS[@]}" "$PI_HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"   # systemctl --user needs this over ssh
cd ~/pidisplay/src

if ! command -v podman >/dev/null; then
    echo "podman is not installed. Run this once, then re-run deploy.sh:" >&2
    echo "  sudo apt update && sudo apt install -y podman" >&2
    exit 1
fi

# Generate the web password once and keep it across deploys.
# Every stage here consumes all of its input: piping from a process into a
# short `head` would SIGPIPE the writer, which `pipefail` turns into a failure.
if [ ! -f ~/pidisplay/auth.env ]; then
    pass="${PIDISPLAY_PASS:-$(head -c 24 /dev/urandom | base64 | tr -dc 'a-z0-9' | cut -c1-14)}"
    printf 'AUTH_PASS=%s\n' "$pass" > ~/pidisplay/auth.env
    chmod 600 ~/pidisplay/auth.env
    echo "generated a new web password"
fi

podman build -t pidisplay:latest -f Containerfile .

echo
echo "--- self-checks (inside the image, same deps as production) ---"
podman run --rm pidisplay:latest python3 test_display.py

mkdir -p ~/.config/containers/systemd
cp pidisplay.container ~/.config/containers/systemd/
loginctl enable-linger "$USER" >/dev/null 2>&1 || true
systemctl --user daemon-reload
systemctl --user restart pidisplay.service
sleep 4
systemctl --user --no-pager --lines=15 status pidisplay.service || true

echo
echo "user:     $(sed -n 's/^Environment=AUTH_USER=//p' ~/.config/containers/systemd/pidisplay.container)"
echo "password: $(sed -n 's/^AUTH_PASS=//p' ~/pidisplay/auth.env)"
REMOTE

say "done - open http://${PI_HOST#*@}:8080"
