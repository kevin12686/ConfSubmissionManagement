#!/bin/sh
set -eu

live_config="/etc/nginx/conf.d/default.conf"
backup_config="/tmp/default.conf.sms-backup"

cp "$live_config" "$backup_config"

restore_config() {
    cp "$backup_config" "$live_config"
}

if ! /docker-entrypoint.d/20-envsubst-on-templates.sh; then
    restore_config
    exit 1
fi

if ! nginx -t; then
    restore_config
    exit 1
fi

if ! nginx -s reload; then
    restore_config
    exit 1
fi

rm -f "$backup_config"
