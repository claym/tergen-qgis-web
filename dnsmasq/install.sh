#!/usr/bin/env bash
# Idempotent dnsmasq installer for *.devbox wildcard DNS.
# Re-runnable: copying the same conf is a no-op; restarting is cheap.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_SRC="${SCRIPT_DIR}/devbox.conf"
CONF_DST="/etc/dnsmasq.d/devbox.conf"

echo "==> Installing dnsmasq via apt"
DEBIAN_FRONTEND=noninteractive apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y dnsmasq

echo "==> Disabling dnsmasq's stub resolver behavior conflicts"
# On Ubuntu, dnsmasq's default config tries to be a system resolver.
# We override only via /etc/dnsmasq.d/, so make sure that drop-in dir is read.
if ! grep -q '^conf-dir=/etc/dnsmasq.d' /etc/dnsmasq.conf 2>/dev/null; then
  echo "  (dnsmasq.conf already includes /etc/dnsmasq.d by default on Ubuntu)"
fi

echo "==> Writing ${CONF_DST}"
install -m 0644 "${CONF_SRC}" "${CONF_DST}"

echo "==> Validating config"
dnsmasq --test

echo "==> Enabling and restarting dnsmasq"
systemctl enable dnsmasq
systemctl restart dnsmasq

echo "==> Waiting for dnsmasq to bind"
sleep 1
systemctl is-active dnsmasq

echo "==> Verifying wildcard resolution"
if dig +short @192.168.1.70 qgis.devbox | grep -q '^192\.168\.1\.70$'; then
  echo "  qgis.devbox -> 192.168.1.70 ✓"
else
  echo "  qgis.devbox did not resolve to 192.168.1.70" >&2
  exit 1
fi
if dig +short @192.168.1.70 anything-else.devbox | grep -q '^192\.168\.1\.70$'; then
  echo "  anything-else.devbox -> 192.168.1.70 ✓"
else
  echo "  wildcard match failed" >&2
  exit 1
fi
if dig +short @192.168.1.70 google.com | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "  upstream resolution working ✓"
else
  echo "  upstream resolution failed" >&2
  exit 1
fi

echo
echo "Done. Configure clients to use 192.168.1.70 (or 100.117.43.89 over Tailscale)"
echo "as their DNS server, OR add an /etc/hosts fallback entry."
