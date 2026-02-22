#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${1:?Usage: $0 <domain>}"
APP_DIR="/opt/htos"
SERVICE="htos"

echo "=== HTOS Web — Deployment to $DOMAIN ==="

# 1. System packages
echo "[1/7] Installing system packages..."
apt update -qq
apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx ufw

# 2. Copy app files
echo "[2/7] Setting up application in $APP_DIR..."
mkdir -p "$APP_DIR"
rsync -a --exclude '.venv' --exclude '__pycache__' --exclude '*.db' --exclude 'workspace/' \
    "$(dirname "$(readlink -f "$0")")/" "$APP_DIR/"

# 3. Python environment
echo "[3/7] Installing Python dependencies..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
"$APP_DIR/.venv/bin/pip" install hypercorn -q

# 4. Production .env (generate secrets on first deploy only)
if [ ! -f "$APP_DIR/.env" ]; then
    echo "[3.5/7] Generating production .env..."
    SECRET=$("$APP_DIR/.venv/bin/python" -c "import secrets; print(secrets.token_urlsafe(32))")
    SIGNING=$("$APP_DIR/.venv/bin/python" -c "import secrets; print(secrets.token_urlsafe(32))")
    cat > "$APP_DIR/.env" <<EOF
SECRET_KEY=$SECRET
DATABASE_PATH=$APP_DIR/htos_web.db
WORKER_SIGNING_KEY=$SIGNING
EOF
    echo "    .env created with fresh secrets."
else
    echo "    .env already exists, skipping."
fi

# 5. Systemd service
echo "[4/7] Creating systemd service..."
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=HTOS Web
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/hypercorn "app:create_app()" --bind 127.0.0.1:5000 --workers 2
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$APP_DIR/workspace/uploads" "$APP_DIR/workspace/results" "$APP_DIR/workspace/processing"
chown -R www-data:www-data "$APP_DIR"

systemctl daemon-reload
systemctl enable --now "$SERVICE"

# 6. Nginx
echo "[5/7] Configuring Nginx..."
cat > /etc/nginx/sites-available/${SERVICE} <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 2G;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/${SERVICE} /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# 7. SSL + Firewall
echo "[6/7] Setting up firewall..."
ufw allow 22
ufw allow 80
ufw allow 443
ufw --force enable

echo "[7/7] Obtaining SSL certificate..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email

echo ""
echo "=== Deployment complete ==="
echo "    Site:    https://$DOMAIN"
echo "    Service: sudo systemctl status $SERVICE"
echo "    Logs:    sudo journalctl -u $SERVICE -f"
echo "    Restart: sudo systemctl restart $SERVICE"
echo ""
echo "Next steps:"
echo "    1. Create an admin user:  cd $APP_DIR && .venv/bin/python admin.py adduser <username>"
echo "    2. Place cecie.pkg in:    $APP_DIR/static/cecie.pkg"
