#!/usr/bin/env bash
# One self-signed cert, 10yr validity — LAN-only tool, not cert-hygiene-purity.
# Shared by the panel and mosquitto so there's one thing to trust, not two.
# Run from nvr-rk3576:  scripts/gen_tls_cert.sh
set -e
cd "$(dirname "$0")/.."

if [ -f config/panel.crt ] && [ -f config/panel.key ]; then
    echo "config/panel.crt + config/panel.key already exist; not overwriting."
    echo "Delete them first if you want to regenerate."
    exit 0
fi

mkdir -p config
# SAN is required (not just CN): both paho-mqtt (the panel) and mosquitto_sub
# verify the hostname against the cert by default, and a CN-only cert fails
# that check for any connect address. Loopback + localhost + the hostname are
# covered here; if you ever connect the panel/broker via a specific LAN IP,
# add it here (e.g. IP:192.168.1.30) before generating.
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout config/panel.key -out config/panel.crt \
  -days 3650 -subj "/CN=falcon-nvr" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost,DNS:falcon-nvr"

echo "generated config/panel.crt + config/panel.key (gitignored, not committed)"
echo "SAN: IP:127.0.0.1, DNS:localhost, DNS:falcon-nvr"
