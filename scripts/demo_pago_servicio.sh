#!/usr/bin/env bash
# Demostración de punta a punta: pago de servicio público de un cliente que falla porque la entidad de recaudo
# responde HTTP 500. Envía el pago, muestra lo que vive el cliente y ejecuta el agente de RCA sobre su código
# de transacción. El nombre del cliente solo existe en el canal: nunca llega a la telemetría.
#
#   scripts/demo_pago_servicio.sh            (requiere contexto kubectl del clúster; abre sus propios port-forwards)
set -euo pipefail

CTX="${KUBE_CONTEXT:-bancoplus-eks}"
CLIENTE_NOMBRE="${CLIENTE_NOMBRE:-Pepe Ramirez}"
CLIENTE_DOCUMENTO="${CLIENTE_DOCUMENTO:-1032456789}"
CONVENIO="${CONVENIO:-ELECTROPLUS}"
MONTO="${MONTO:-220000}"
APP_PORT=18080
TEMPO_PORT=13200
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-$DIR/.venv/bin/python}"
TMP="$(mktemp -d)"

kubectl --context "$CTX" -n pagos port-forward svc/payments-qr "$APP_PORT:80" >/dev/null 2>&1 &
PF_APP=$!
kubectl --context "$CTX" -n observability port-forward svc/tempo "$TEMPO_PORT:3200" >/dev/null 2>&1 &
PF_TEMPO=$!
trap 'kill $PF_APP $PF_TEMPO 2>/dev/null; rm -rf "$TMP"' EXIT
sleep 4

echo "== 1. Lo que vive el cliente (canal: app móvil)"
echo "   ${CLIENTE_NOMBRE} · CC ${CLIENTE_DOCUMENTO} · paga \$${MONTO} a ${CONVENIO}"
# El scoring de fraude puede declinar (≈10 %): se reintenta hasta reproducir la caída de la entidad
for intento in 1 2 3 4 5; do
  body=$(printf '{"amount": %s, "cardNumber": "4111111111111111", "channel": "app-movil", "billerCode": "%s", "customerDocument": "%s", "customerName": "%s"}' \
    "$MONTO" "$CONVENIO" "$CLIENTE_DOCUMENTO" "$CLIENTE_NOMBRE")
  code=$(curl -s -o "$TMP/body" -D "$TMP/headers" -w '%{http_code}' -X POST "http://localhost:$APP_PORT/payments/qr" \
    -H 'Content-Type: application/json' -d "$body")
  reason=$(grep -i '^x-business-reason:' "$TMP/headers" | tr -d '\r' | awk '{print $2}')
  txn=$(grep -i '^x-business-transaction-id:' "$TMP/headers" | tr -d '\r' | awk '{print $2}')
  echo "   intento $intento · HTTP $code · motivo ${reason:-?} · transacción ${txn:-?}"
  [[ "$reason" == "CONVENIO_NO_DISPONIBLE" ]] && break
done
echo "   respuesta al cliente: $(cat "$TMP/body")"
[[ "$reason" == "CONVENIO_NO_DISPONIBLE" ]] || { echo "No se reprodujo la caída del convenio" >&2; exit 1; }

echo
echo "== 2. Diagnóstico del agente de RCA (Tempo vía MCP, solo lectura)"
echo "   esperando la decisión de tail sampling y la exportación de la traza..."
for _ in $(seq 1 12); do
  if TEMPO_MCP_URL="http://localhost:$TEMPO_PORT/api/mcp" "$PY" "$DIR/scripts/sre_rca_agent.py" \
       --transaction-id "$txn" --window 15 >"$TMP/rca" 2>"$TMP/rca.log"; then
    cat "$TMP/rca"
    exit 0
  fi
  sleep 5
done
cat "$TMP/rca.log" >&2
exit 1
