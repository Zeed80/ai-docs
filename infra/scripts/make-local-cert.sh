#!/usr/bin/env bash
# Locally-trusted TLS certificate for the stand.
#
# Traefik serves its untrusted built-in certificate for anything that is not the
# ACME domain, so https://localhost is refused by the browser outright
# (ERR_CERT_AUTHORITY_INVALID) and the service worker never registers. This
# script issues a small CA plus a server certificate covering localhost, the
# machine's LAN address and the deployment domain, which Traefik then uses as
# its default certificate.
#
#   ./infra/scripts/make-local-cert.sh [extra-dns-name ...]
#
# Import infra/traefik/certs/local-ca.crt once into the browser (or the OS
# trust store) on every machine that opens the stand.
set -euo pipefail

CERT_DIR="$(cd "$(dirname "$0")/.." && pwd)/traefik/certs"
mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

{
  echo "[req]"
  echo "distinguished_name = dn"
  echo "req_extensions = v3_req"
  echo "prompt = no"
  echo
  echo "[dn]"
  echo "CN = localhost"
  echo "O = AI-DOCS"
  echo
  echo "[v3_req]"
  echo "basicConstraints = CA:FALSE"
  echo "keyUsage = critical,digitalSignature,keyEncipherment"
  echo "extendedKeyUsage = serverAuth"
  echo "subjectAltName = @alt"
  echo
  echo "[alt]"
  echo "DNS.1 = localhost"
  index=2
  # The deployment domain is deliberately NOT listed here. Traefik prefers a
  # configured certificate over an ACME one for the same SNI, so adding the
  # domain made https://<domain> serve this local certificate instead of the
  # Let's Encrypt one — a production outage in exchange for a console warning.
  for name in "$@"; do
    echo "DNS.${index} = ${name}"; index=$((index + 1))
  done
  echo "IP.1 = 127.0.0.1"
  [ -n "${LAN_IP:-}" ] && echo "IP.2 = ${LAN_IP}"
} > openssl-server.cnf

if [ ! -f local-ca.crt ]; then
  cat > openssl-local.cnf <<'CNF'
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no

[dn]
CN = AI-DOCS local CA
O = AI-DOCS
C = RU

[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
CNF
  openssl genrsa -out local-ca.key 4096
  openssl req -x509 -new -nodes -key local-ca.key -sha256 -days 3650 \
    -config openssl-local.cnf -out local-ca.crt
  echo "Создан новый локальный УЦ: $CERT_DIR/local-ca.crt"
else
  echo "Использую существующий УЦ: $CERT_DIR/local-ca.crt"
fi

openssl genrsa -out local.key 2048
openssl req -new -key local.key -config openssl-server.cnf -out local.csr
# 825 days is the maximum lifetime browsers accept for a server certificate.
openssl x509 -req -in local.csr -CA local-ca.crt -CAkey local-ca.key -CAcreateserial \
  -out local.crt -days 825 -sha256 -extfile openssl-server.cnf -extensions v3_req
rm -f local.csr local-ca.srl
chmod 600 local.key local-ca.key

echo
openssl x509 -in local.crt -noout -subject -dates -ext subjectAltName
echo
echo "Готово. Дальше:"
echo "  1) перезапустить traefik:  docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml up -d traefik"
echo "  2) импортировать $CERT_DIR/local-ca.crt в браузер (см. infra/README-local-tls.md)"
