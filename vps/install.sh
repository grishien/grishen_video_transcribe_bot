#!/usr/bin/env bash
# Install the inbox receiver. Does not edit or restart Amnezia or frp.
# Usage: sudo bash install.sh --token TOKEN --port PORT [--max-size-mb 500]

set -euo pipefail

APP_USER="transcribe-inbox"
APP_DIR="/opt/transcribe-inbox"
INBOX_DIR="/var/lib/transcribe-inbox"
ENV_FILE="/etc/transcribe-inbox.env"
UNIT_NAME="transcribe-inbox.service"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TOKEN=""
PORT=""
MAX_SIZE_MB="500"

usage() {
  cat <<'EOF'
Usage: sudo bash install.sh --token TOKEN --port PORT [--max-size-mb 500]

TOKEN is at least 32 characters: letters, digits, underscore, hyphen.
Generate one with:
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"

PORT is a free TCP port. See listeners with: sudo ss -tlnp
Do not reuse a port used by SSH, Amnezia, frp, or anything else.

The script installs the app, writes the root-only env file, and starts only
transcribe-inbox. If ufw or firewalld is already active, it opens this port.
It does not enable a firewall that is off, and it does not change other rules.
EOF
}

die() {
  printf '%s\n' "$1" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)
      [[ $# -ge 2 ]] || die "--token needs a value"
      TOKEN="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || die "--port needs a value"
      PORT="$2"
      shift 2
      ;;
    --max-size-mb)
      [[ $# -ge 2 ]] || die "--max-size-mb needs a value"
      MAX_SIZE_MB="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  die "Run as root: sudo bash install.sh --token ... --port ..."
fi

if [[ -z "$TOKEN" ]]; then
  [[ -t 0 ]] || die "Pass --token. There is no terminal to ask for it."
  read -r -p "TOKEN: " TOKEN
fi
if [[ -z "$PORT" ]]; then
  [[ -t 0 ]] || die "Pass --port. There is no terminal to ask for it."
  read -r -p "PORT: " PORT
fi

[[ "$TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]] || die "TOKEN must be at least 32 characters and use only letters, digits, underscore, and hyphen."
[[ "$PORT" =~ ^[0-9]+$ ]] || die "PORT must be an integer from 1 to 65535."
[[ "$MAX_SIZE_MB" =~ ^[0-9]+$ ]] || die "MAX_SIZE_MB must be an integer >= 1."
PORT=$((10#$PORT))
MAX_SIZE_MB=$((10#$MAX_SIZE_MB))
(( PORT >= 1 && PORT <= 65535 )) || die "PORT must be an integer from 1 to 65535."
(( MAX_SIZE_MB >= 1 )) || die "MAX_SIZE_MB must be an integer >= 1."

[[ -f "$SOURCE/main.py" && -f "$SOURCE/requirements.txt" && -f "$SOURCE/deploy/transcribe-inbox.service" ]] || die "Run this script from the vps directory that contains main.py, requirements.txt, and deploy/transcribe-inbox.service."

command -v python3 >/dev/null || die "python3 is not installed. Python 3.10 or newer is required."
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || die "Python 3.10 or newer is required."
python3 -c 'import venv' >/dev/null 2>&1 || die "python3-venv is missing. On Debian/Ubuntu: sudo apt install python3-venv"
command -v ss >/dev/null || die "ss is not installed (package iproute2). Use it to pick a free port: sudo ss -tlnp"

our_pid=""
if systemctl is-active --quiet "$UNIT_NAME"; then
  our_pid="$(systemctl show -p MainPID --value "$UNIT_NAME" || true)"
fi
listeners="$(ss -tlnpH "sport = :${PORT}" || true)"
if [[ -n "$listeners" ]]; then
  foreign="$listeners"
  if [[ -n "$our_pid" && "$our_pid" != "0" ]]; then
    foreign="$(printf '%s\n' "$listeners" | grep -v "pid=${our_pid}" || true)"
  fi
  if [[ -n "$foreign" ]]; then
    printf 'Port %s is already in use:\n%s\n' "$PORT" "$listeners" >&2
    die "Pick another port. Do not stop Amnezia, frp, or any other service."
  fi
fi

nologin="$(command -v nologin || true)"
[[ -n "$nologin" ]] || nologin="/usr/sbin/nologin"
[[ -x "$nologin" ]] || die "nologin shell not found. Install it or create the user by hand."

if ! getent group "$APP_USER" >/dev/null; then
  groupadd --system "$APP_USER"
fi
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --gid "$APP_USER" --no-create-home --shell "$nologin" "$APP_USER"
fi

mkdir -p "$APP_DIR" "$INBOX_DIR"
cp "$SOURCE/main.py" "$SOURCE/requirements.txt" "$APP_DIR/"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR"
chmod -R u=rwX,go=rX "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$INBOX_DIR"
chmod 700 "$INBOX_DIR"

umask 077
cat > "$ENV_FILE" <<EOF
TOKEN=${TOKEN}
INBOX_DIR=${INBOX_DIR}
MAX_SIZE_MB=${MAX_SIZE_MB}
PORT=${PORT}
EOF
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"

cp "$SOURCE/deploy/transcribe-inbox.service" "/etc/systemd/system/${UNIT_NAME}"
systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl restart "$UNIT_NAME"

if command -v ufw >/dev/null; then
  ufw_state="$(ufw status 2>/dev/null | awk 'NR==1 { print; exit }' || true)"
  if [[ "$ufw_state" == "Status: active" ]]; then
    ufw allow "${PORT}/tcp" comment 'transcribe-inbox'
    printf 'Opened TCP %s in the active ufw firewall.\n' "$PORT"
  fi
fi
if systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-port="${PORT}/tcp"
  firewall-cmd --reload
  printf 'Opened TCP %s in the active firewalld firewall.\n' "$PORT"
fi

printf '\ntranscribe-inbox is listening on 0.0.0.0:%s\n' "$PORT"
printf 'If the hosting provider has a firewall or security group, allow inbound TCP %s there too.\n' "$PORT"
printf 'Amnezia and frp were not modified.\n'
systemctl --no-pager --full status "$UNIT_NAME"
