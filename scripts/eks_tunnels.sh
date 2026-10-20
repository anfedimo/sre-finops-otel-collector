#!/usr/bin/env bash
# Túneles efímeros hacia el clúster EKS para las demostraciones: cada comando abre sus propios port-forwards en
# puertos libres, espera a que el servicio responda y los cierra al terminar. No depende de terminales abiertas.
#
#   scripts/eks_tunnels.sh <comando...>      # exporta TEMPO_MCP_URL y PROM_URL y ejecuta el comando
#   source scripts/eks_tunnels.sh            # expone open_tunnel para otros scripts
#
# Si TEMPO_MCP_URL o PROM_URL ya vienen definidos, se respetan y no se abre ese túnel.
set -euo pipefail

CTX="${KUBE_CONTEXT:-bancoplus-eks}"
_TUNNEL_PIDS=()

_free_port() {
  python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

_close_tunnels() {
  local pid
  for pid in "${_TUNNEL_PIDS[@]:-}"; do [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true; done
}
trap _close_tunnels EXIT

# open_tunnel <namespace> <svc> <puerto-remoto> <ruta-de-salud> → deja en TUNNEL_PORT el puerto local cuando el
# servicio responde. Sin $(...): el PID debe quedar registrado en este shell para cerrarlo al salir.
open_tunnel() {
  local ns="$1" svc="$2" remote="$3" health="$4" port pid
  port="$(_free_port)"
  kubectl --context "$CTX" -n "$ns" port-forward "svc/$svc" "$port:$remote" >/dev/null 2>&1 &
  pid=$!
  disown "$pid"   # sin avisos de job control al cerrar el túnel
  _TUNNEL_PIDS+=("$pid")
  for _ in $(seq 1 40); do
    if curl -s -o /dev/null -m 2 "http://localhost:$port$health"; then
      TUNNEL_PORT="$port"
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  echo "túnel a $ns/$svc no disponible: revisa 'kubectl --context $CTX get nodes' y tu IP autorizada" >&2
  return 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  [[ $# -gt 0 ]] || { echo "uso: $0 <comando...>" >&2; exit 2; }
  if [[ -z "${TEMPO_MCP_URL:-}" ]]; then
    open_tunnel observability tempo 3200 /ready
    TEMPO_MCP_URL="http://localhost:$TUNNEL_PORT/api/mcp"
  fi
  if [[ -z "${PROM_URL:-}" ]]; then
    open_tunnel observability kube-prometheus-stack-prometheus 9090 /-/ready
    PROM_URL="http://localhost:$TUNNEL_PORT"
  fi
  export TEMPO_MCP_URL PROM_URL
  "$@"
fi
