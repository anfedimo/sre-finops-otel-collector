#!/usr/bin/env bash
# Despliegue y rollback de payments-qr sobre el entorno local (docker compose).
#   deploy.sh <version> <stable|regression>
#   deploy.sh --rollback
# El perfil emula el comportamiento del binario liberado; en el entorno real se reemplaza
# por el rollout del chart Helm / Argo Rollouts sin cambiar el contrato del pipeline.
set -euo pipefail

STATE_DIR=".deploy"
STATE_FILE="$STATE_DIR/state"
mkdir -p "$STATE_DIR"

CURRENT_VERSION="1.0.0"; CURRENT_PROFILE="stable"
PREVIOUS_VERSION="";     PREVIOUS_PROFILE=""
# shellcheck disable=SC1090
[[ -f "$STATE_FILE" ]] && source "$STATE_FILE"

profile_env() {
  case "$1" in
    stable)     echo "P_TECH_ERROR=0.0001 P_HIDDEN_ERROR_200=0.0001 P_SLOW=0.002" ;;
    # Regresión invisible para monitoreo por código HTTP: fallas devueltas como 200
    regression) echo "P_TECH_ERROR=0.005 P_HIDDEN_ERROR_200=0.04 P_SLOW=0.03" ;;
    *) echo "[deploy ] perfil desconocido: $1" >&2; exit 2 ;;
  esac
}

apply() {
  local version="$1" profile="$2"
  # shellcheck disable=SC2046
  env $(profile_env "$profile") APP_VERSION="$version" \
    docker compose up -d --no-deps traffic-generator
  echo "[deploy ] payments-qr version=$version profile=$profile"
}

save_state() {
  cat > "$STATE_FILE" <<EOF
CURRENT_VERSION="$1"
CURRENT_PROFILE="$2"
PREVIOUS_VERSION="$3"
PREVIOUS_PROFILE="$4"
EOF
}

if [[ "${1:-}" == "--rollback" ]]; then
  if [[ -z "$PREVIOUS_VERSION" ]]; then
    echo "[rollback] sin versión previa registrada" >&2
    exit 1
  fi
  echo "[rollback] $CURRENT_VERSION → $PREVIOUS_VERSION"
  apply "$PREVIOUS_VERSION" "$PREVIOUS_PROFILE"
  save_state "$PREVIOUS_VERSION" "$PREVIOUS_PROFILE" "" ""
  exit 0
fi

VERSION="${1:?uso: deploy.sh <version> <stable|regression> | --rollback}"
PROFILE="${2:-stable}"
profile_env "$PROFILE" >/dev/null
echo "[deploy ] $CURRENT_VERSION → $VERSION"
apply "$VERSION" "$PROFILE"
save_state "$VERSION" "$PROFILE" "$CURRENT_VERSION" "$CURRENT_PROFILE"
