#!/bin/sh
# Повторяемая установка/обновление на Selectel. Запуск от root из распакованного релиза:
#   sh deploy/install.sh /tmp/suppliers-release
# Данные (data/) и ключи (/etc/suppliers.env) не трогает.
set -eu
SRC=${1:?release dir}
APP=/opt/suppliers

id suppliers >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin suppliers
mkdir -p "$APP/data" "$APP/templates" "$APP/static"
cp "$SRC"/*.py "$SRC"/categories.yaml "$SRC"/requirements.txt "$APP"/
cp "$SRC"/templates/*.html "$APP"/templates/
cp "$SRC"/static/* "$APP"/static/
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R suppliers:suppliers "$APP"
[ -f /etc/suppliers.env ] || install -m 600 /dev/null /etc/suppliers.env   # ключи вписать вручную (systemd читает от root)

cp "$SRC"/deploy/suppliers.service "$SRC"/deploy/suppliers-collect.service /etc/systemd/system/
install -m 644 "$SRC"/deploy/suppliers.alexmedvedev.com.caddy /srv/caddy/suppliers.alexmedvedev.com.caddy
ufw status | grep -q "8766/tcp.*172.18.0.0/16" || ufw allow from 172.18.0.0/16 to any port 8766 proto tcp comment 'caddy (docker) -> suppliers'
systemctl daemon-reload
systemctl enable suppliers >/dev/null
systemctl restart suppliers
CADDY=$(docker ps -qf name=caddy | head -1)   # общий Caddy сервера в Docker, импортирует /srv/caddy/*.caddy
docker exec "$CADDY" caddy reload --config /etc/caddy/Caddyfile
echo "ok: https://suppliers.alexmedvedev.com ; сбор: systemctl start suppliers-collect ; журнал: journalctl -u suppliers-collect -f"
