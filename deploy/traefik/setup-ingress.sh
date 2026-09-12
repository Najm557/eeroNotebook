#!/bin/bash
# Register eeroNotebook with the Dev Server's existing reverse proxy.
#
# mon-traefik already terminates TLS for the host and already holds 80/443/8080,
# so eeroNotebook joins it rather than running a proxy of its own
# (Requirements 13.3, 13.5). mon-traefik reads a watched directory of dynamic
# configuration files and no Docker labels, so registering means placing a file
# and a certificate in that directory. It reloads on its own; nothing restarts.
#
# Run this ON the Dev Server, from the deployed stack:
#
#   ~/stacks/eeronotebook/deploy/traefik/setup-ingress.sh
#
# Idempotent. Safe to re-run after changing EERONOTEBOOK_DOMAIN or the template.

set -euo pipefail

TRAEFIK_SRC="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$TRAEFIK_SRC/../.env"
CERT_DIR="$TRAEFIK_SRC/certs"

# mon-traefik mounts this directory at /etc/traefik/dynamic. Overridable so this
# is testable and so relocating the monitoring stack does not mean editing here.
TRAEFIK_DYNAMIC_DIR="${TRAEFIK_DYNAMIC_DIR:-$HOME/stacks/monitoring/traefik}"

if [ ! -d "$TRAEFIK_DYNAMIC_DIR" ]; then
  echo "error: $TRAEFIK_DYNAMIC_DIR not found." >&2
  echo "       This script must run on the Dev Server, where the monitoring stack lives." >&2
  exit 1
fi

DOMAIN="$(grep -E '^EERONOTEBOOK_DOMAIN=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' \r" || true)"
DOMAIN="${DOMAIN:-eeronotebook.local}"

echo "=== Registering eeroNotebook with mon-traefik ==="
echo "  domain: $DOMAIN"
echo "  target: $TRAEFIK_DYNAMIC_DIR"
echo ""

# --- Step 1: certificate ----------------------------------------------------
if [ -f "$CERT_DIR/eeronotebook.crt" ] && [ -f "$CERT_DIR/eeronotebook.key" ]; then
  echo "Step 1: certificate already present, skipping generation."
else
  echo "Step 1: generating the certificate..."
  bash "$CERT_DIR/generate-certs.sh"
fi

# --- Step 2: install cert and routing --------------------------------------
echo ""
echo "Step 2: installing into mon-traefik's dynamic directory..."

# Alongside learninglab.crt/.key, which is where this host already keeps the
# proxy's certificates: mon-traefik mounts only this one directory.
install -m 644 "$CERT_DIR/eeronotebook.crt" "$TRAEFIK_DYNAMIC_DIR/eeronotebook.crt"
install -m 600 "$CERT_DIR/eeronotebook.key" "$TRAEFIK_DYNAMIC_DIR/eeronotebook.key"

sed "s/__DOMAIN__/$DOMAIN/g" "$TRAEFIK_SRC/eeronotebook.yml.template" \
  > "$TRAEFIK_DYNAMIC_DIR/eeronotebook.yml"

echo "  ✓ eeronotebook.crt, eeronotebook.key, eeronotebook.yml"
echo ""
echo "mon-traefik watches this directory and picks the change up within seconds."
echo ""
echo "=== Remaining operator steps ==="
echo ""
echo "1. Resolve the name. There is no DNS server on this network yet, so add to"
echo "   /etc/hosts on the Dev Server and on every client device:"
echo ""
echo "     $(grep -E '^EERONOTEBOOK_HOST=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d "\"' \r")  $DOMAIN api.$DOMAIN"
echo ""
echo "2. Trust the CA, or browsers will warn on every visit. Copy"
echo "   $CERT_DIR/ca-cert.pem to each client — the CA key stays here — then on macOS:"
echo ""
echo "     sudo security add-trusted-cert -d -r trustRoot \\"
echo "       -k /Library/Keychains/System.keychain ca-cert.pem"
echo ""
echo "   iOS/iPadOS: install the profile, then enable it under"
echo "   Settings > General > About > Certificate Trust Settings."
echo ""
echo "URLs once both are done:"
echo "  UI:   https://$DOMAIN"
echo "  API:  https://api.$DOMAIN"
