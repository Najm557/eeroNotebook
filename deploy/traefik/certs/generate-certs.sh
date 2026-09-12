#!/bin/bash
# Generate a private CA and a TLS certificate for eeroNotebook.
#
# Follows the pattern already established in EarWig/traefik/certs, with two
# additions modern clients require and that script predates: an
# extendedKeyUsage of serverAuth, and a leaf lifetime inside the 825-day limit
# Apple and Chrome enforce.
#
# Run this ON the Dev Server. The CA private key is a real secret and should
# never transit a client machine.
#
#   ./generate-certs.sh
#
# Reads from deploy/.env, so the Dev Server's address is defined in exactly one
# place (Requirement 13.7):
#   EERONOTEBOOK_HOST    — IP SAN, so the certificate also covers access by address
#   EERONOTEBOOK_DOMAIN  — DNS SANs, and the name the Traefik routers match on

set -euo pipefail

CERT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$CERT_DIR/../../.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE not found. Copy deploy/.env.example to deploy/.env first." >&2
  exit 1
fi

# Read the two values without sourcing the file: .env also holds secrets, and
# nothing here needs them.
read_env() {
  grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d "\"' \r"
}

HOST_IP="$(read_env EERONOTEBOOK_HOST)"
DOMAIN="$(read_env EERONOTEBOOK_DOMAIN)"
DOMAIN="${DOMAIN:-eeronotebook.local}"

if [ -z "$HOST_IP" ]; then
  echo "error: EERONOTEBOOK_HOST is not set in $ENV_FILE" >&2
  exit 1
fi

# LibreSSL ships as `openssl` on macOS and is missing options used below.
# Prefer real OpenSSL when Homebrew has it.
OPENSSL=openssl
for candidate in /opt/homebrew/bin/openssl /usr/local/bin/openssl; do
  if [ -x "$candidate" ]; then OPENSSL="$candidate"; break; fi
done

echo "=== eeroNotebook certificate generation ==="
echo "  openssl:  $($OPENSSL version)"
echo "  domain:   $DOMAIN, api.$DOMAIN"
echo "  address:  $HOST_IP"
echo "  output:   $CERT_DIR"
echo ""

# --- Private CA -------------------------------------------------------------
# Reused if it already exists: regenerating it would invalidate the copy every
# client device has been asked to trust.
if [ -f "$CERT_DIR/ca-cert.pem" ] && [ -f "$CERT_DIR/ca-key.pem" ]; then
  echo "Reusing the existing CA. Delete ca-cert.pem and ca-key.pem to start over,"
  echo "and expect to redistribute the new CA to every client device."
else
  echo "Creating the private CA..."
  "$OPENSSL" genrsa -out "$CERT_DIR/ca-key.pem" 4096
  "$OPENSSL" req -new -x509 -key "$CERT_DIR/ca-key.pem" \
    -out "$CERT_DIR/ca-cert.pem" \
    -days 3650 -sha256 \
    -subj "/C=US/ST=Local/L=Dev/O=eeroNotebook/CN=eeroNotebook Local CA"
fi
chmod 600 "$CERT_DIR/ca-key.pem"

# --- Server certificate -----------------------------------------------------
echo "Creating the server certificate..."
"$OPENSSL" genrsa -out "$CERT_DIR/eeronotebook.key" 2048

cat > "$CERT_DIR/san.cnf" <<EOF
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
C = US
ST = Local
L = Dev
O = eeroNotebook
CN = $DOMAIN

[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = $DOMAIN
DNS.2 = api.$DOMAIN
DNS.3 = *.$DOMAIN
IP.1 = $HOST_IP
EOF

"$OPENSSL" req -new -key "$CERT_DIR/eeronotebook.key" \
  -out "$CERT_DIR/eeronotebook.csr" \
  -config "$CERT_DIR/san.cnf"

# 825 days: Apple and Chrome reject longer-lived leaf certificates. The CA above
# is long-lived precisely so the leaf does not have to be.
"$OPENSSL" x509 -req -in "$CERT_DIR/eeronotebook.csr" \
  -CA "$CERT_DIR/ca-cert.pem" \
  -CAkey "$CERT_DIR/ca-key.pem" \
  -CAcreateserial \
  -out "$CERT_DIR/eeronotebook.crt" \
  -days 825 -sha256 \
  -extensions v3_req \
  -extfile "$CERT_DIR/san.cnf"

rm -f "$CERT_DIR/eeronotebook.csr" "$CERT_DIR/san.cnf" "$CERT_DIR/ca-cert.srl"
chmod 600 "$CERT_DIR/eeronotebook.key"

echo ""
echo "Done."
"$OPENSSL" x509 -in "$CERT_DIR/eeronotebook.crt" -noout -subject -issuer -dates
echo ""
echo "  ca-cert.pem         private CA — distribute this to client devices"
echo "  ca-key.pem          private CA key — keep on this host only, never copy"
echo "  eeronotebook.crt    server certificate"
echo "  eeronotebook.key    server private key"
