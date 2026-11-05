#!/usr/bin/env bash
# One-time VPS setup (Ubuntu/Debian). Usage:  sudo ./deploy/install.sh texts.yourdomain.com
# Installs Python deps, Caddy (HTTPS via Let's Encrypt) and the systemd unit.
# It does NOT start texting: the service is started only when you run `sudo systemctl enable --now close-outbound`.
set -euo pipefail

DOMAIN="${1:-}"
[[ -n "$DOMAIN" ]] || { echo "Usage: sudo $0 <domain>"; exit 1; }
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER=outbound
PORT=8000

case "$APP_DIR" in
  /home/*|/root/*) echo "Clone the repo to /opt/close-outbound-setup (the service can't run from a home folder)."; exit 1 ;;
esac

echo "==> Packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl gpg debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Caddy"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> Service user and Python environment"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
[[ -f "$APP_DIR/.env" ]] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> 'outbound' command"
cat > /usr/local/bin/outbound <<SCRIPT
#!/usr/bin/env bash
# Runs the CLI as the service user so files keep the right owner.
cd "$APP_DIR" || exit 1
if [[ "\$(id -un)" == "$APP_USER" ]]; then exec "$APP_DIR/.venv/bin/python" -m outbound "\$@"; fi
exec sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m outbound "\$@"
SCRIPT
chmod 755 /usr/local/bin/outbound

echo "==> Caddy config for $DOMAIN"
mkdir -p /var/log/caddy
sed -e "s/{{DOMAIN}}/$DOMAIN/g" -e "s/{{PORT}}/$PORT/g" "$APP_DIR/deploy/Caddyfile.template" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl restart caddy

echo "==> systemd unit"
sed -e "s#{{APP_DIR}}#$APP_DIR#g" -e "s#{{APP_USER}}#$APP_USER#g" -e "s#{{PORT}}#$PORT#g" \
  "$APP_DIR/deploy/close-outbound.service" > /etc/systemd/system/close-outbound.service
systemctl daemon-reload

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "==> Firewall: allowing 80 and 443"
  ufw allow 80/tcp
  ufw allow 443/tcp
fi

cat <<DONE

Installed. Next steps (see README):
  1. sudo nano $APP_DIR/.env          # CLOSE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  2. outbound doctor                  # checks everything
  3. outbound dry-run                 # who would be texted — review before going live
  4. outbound setup-webhook --url https://$DOMAIN/webhooks/close
  5. sudo systemctl enable --now close-outbound   # GO LIVE
DONE
