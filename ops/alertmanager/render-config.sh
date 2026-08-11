#!/bin/sh
set -eu

base=/etc/alertmanager/alertmanager.yml
rendered=/tmp/alertmanager.yml

if [ -z "${ALERT_WEBHOOK_URL:-}" ]; then
  cp "$base" "$rendered"
else
  # Escape the only characters sed treats specially in this replacement.
  url=$(printf '%s' "$ALERT_WEBHOOK_URL" | sed 's/[\\&|]/\\&/g')
  sed "s|__ALERT_WEBHOOK_URL__|$url|g" /etc/alertmanager/webhook.yml > /tmp/webhook.yml
  awk '
    /# ALERT_WEBHOOK_CONFIG/ {
      while ((getline line < "/tmp/webhook.yml") > 0) print line
      close("/tmp/webhook.yml")
      next
    }
    { print }
  ' "$base" > "$rendered"
fi

exec /bin/alertmanager --config.file="$rendered" --storage.path=/alertmanager
