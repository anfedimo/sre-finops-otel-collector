#!/usr/bin/env bash
# Verificación previa a la demostración en EKS: IP autorizada, clúster, GitOps, tráfico y túneles colgados.
# Solo lectura, salvo cerrar port-forwards de kubectl suspendidos (Ctrl+Z) que retienen puertos locales.
set -uo pipefail

CTX="${KUBE_CONTEXT:-bancoplus-eks}"
fallas=0
ok()   { printf '  [OK]    %s\n' "$1"; }
fail() { printf '  [FALLA] %s\n' "$1"; fallas=$((fallas + 1)); }

echo "== Preparación de la demostración · contexto $CTX"

ip=$(curl -s -m 5 https://checkip.amazonaws.com)
echo "  IP pública: ${ip:-desconocida} (debe estar en admin_cidrs)"

nodos=$(kubectl --context "$CTX" get nodes --no-headers 2>/dev/null | awk '$2 == "Ready"' | wc -l | tr -d ' ')
[[ "$nodos" -ge 1 ]] && ok "$nodos nodos Ready" || fail "sin acceso al clúster: revisa la IP autorizada"

apps=$(kubectl --context "$CTX" -n argocd get applications --no-headers 2>/dev/null)
total=$(printf '%s\n' "$apps" | grep -c . || true)
sanas=$(printf '%s\n' "$apps" | awk '$2 == "Synced" && $3 == "Healthy"' | wc -l | tr -d ' ')
[[ "$total" -gt 0 && "$sanas" -eq "$total" ]] && ok "Argo CD: $sanas/$total aplicaciones Synced/Healthy" \
  || fail "Argo CD: $sanas/$total aplicaciones Synced/Healthy"

replicas=$(kubectl --context "$CTX" -n pagos get deploy payments-qr -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
[[ "$replicas" == "1" ]] && ok "payments-qr: 1 réplica lista (estado declarado en Git)" || fail "payments-qr: ${replicas:-0} réplicas listas"

trafico=$(kubectl --context "$CTX" -n pagos logs job/demo-traffic -c traffic --tail=1 2>/dev/null)
[[ "$trafico" == *requests_min* ]] && ok "tráfico de demostración: $trafico" || fail "tráfico de demostración detenido: make demo-trafico"

colgados=$(ps -axo pid=,stat=,command= | awk '$2 ~ /^T/ && /kubectl/ && /port-forward/ {print $1}')
if [[ -n "$colgados" ]]; then
  kill -9 $colgados 2>/dev/null
  ok "túneles suspendidos con Ctrl+Z cerrados (PID $(echo $colgados))"
else
  ok "sin túneles suspendidos"
fi

echo
[[ "$fallas" -eq 0 ]] && echo "LISTO PARA LA DEMOSTRACIÓN" || { echo "$fallas verificaciones fallidas"; exit 1; }
