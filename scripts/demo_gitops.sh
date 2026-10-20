#!/usr/bin/env bash
# Demo GitOps: un cambio manual no autorizado (escalar a 3 réplicas) y la reconciliación de Argo CD al estado de Git.
# Termina solo: no requiere Ctrl+C.
set -euo pipefail

CTX="${KUBE_CONTEXT:-bancoplus-eks}"
K=(kubectl --context "$CTX" -n pagos)
deseadas=$("${K[@]}" get deploy payments-qr -o jsonpath='{.spec.replicas}')

echo "== Estado declarado en Git: $deseadas réplica(s)"
echo "== Cambio manual no autorizado: kubectl scale deploy/payments-qr --replicas=3"
aplicadas=$("${K[@]}" scale deploy/payments-qr --replicas=3 -o jsonpath='{.spec.replicas}')
printf '   réplicas en el clúster: %s  ← cambio manual aplicado\n' "$aplicadas"
inicio=$(python3 -c 'import time; print(time.time())')

for _ in $(seq 1 120); do
  actual=$("${K[@]}" get deploy payments-qr -o jsonpath='{.spec.replicas}')
  printf '   réplicas en el clúster: %s\n' "$actual"
  if [[ "$actual" == "$deseadas" ]]; then
    seg=$(python3 -c "import time; print(f'{time.time() - $inicio:.1f}')")
    echo "== Argo CD revirtió el cambio manual en ${seg} s: el clúster vuelve a lo aprobado en Git"
    exit 0
  fi
  sleep 0.5
done
echo "== Sin reversión en 60 s: en Argo CD abre payments-qr y pulsa Refresh" >&2
exit 1
