#!/usr/bin/env bash
# Pruebas de punta a punta: levanta la plataforma completa (API, worker, dispatcher, mantenimiento,
# PostgreSQL, Redis, RabbitMQ y nginx) con simuladores de las 8 plataformas y del modelo, corre las
# pruebas y apaga todo, también si fallan. No usa servicios externos ni credenciales reales.
#
#   scripts/e2e.sh                       construye las imágenes, prueba y apaga
#   SKIP_BUILD=1 scripts/e2e.sh          usa las imágenes ya construidas
#   KEEP=1 scripts/e2e.sh                deja el entorno levantado para revisarlo
#   scripts/e2e.sh -k whatsapp           argumentos extra para pytest
#
# Resultado: tests/e2e/results/e2e-junit.xml
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# En Git Bash (Windows) los volúmenes necesitan la ruta con letra de unidad y sin conversión.
export MSYS_NO_PATHCONV=1
HOST_ROOT="$(pwd -W 2>/dev/null || pwd)"
E2E="tests/e2e"
USER_FLAG="$(id -u):$(id -g)"
COMPOSE=(docker compose -p iamia-e2e --env-file "$E2E/.env.e2e"
  -f docker-compose.yml -f "$E2E/compose.e2e.yml" --profile models)

if [[ "${SKIP_BUILD:-0}" != 1 ]]; then
  docker build -f backend/Dockerfile --target runtime -t iamia/backend:1.1.0 .
  docker build -f backend/Dockerfile --target test -t iamia/backend:test .
fi

mkdir -p "$E2E/.dev-keys" "$E2E/results"
# Secretos de prueba propios de esta máquina (tests/e2e/.env.e2e no se versiona).
if [[ ! -f "$E2E/.env.e2e" ]]; then
  docker run --rm --user "$USER_FLAG" -v "$HOST_ROOT/$E2E:/out" iamia/backend:test \
    python /out/make_env.py /out/.env.e2e
fi
# Par de claves que hace de proveedor OIDC para los tokens de la consola.
if [[ ! -f "$E2E/.dev-keys/oidc-dev-public.pem" ]]; then
  docker run --rm --user "$USER_FLAG" -e APP_ENV=development -v "$HOST_ROOT/$E2E/.dev-keys:/keys" \
    iamia/backend:1.1.0 dev-token --key-dir /keys >/dev/null
fi

cleanup() {
  status=$?
  if [[ "${KEEP:-0}" == 1 ]]; then
    echo "Entorno levantado (KEEP=1). Para apagarlo: ${COMPOSE[*]} down -v"
  else
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

"${COMPOSE[@]}" up -d --wait --wait-timeout 300
"${COMPOSE[@]}" run --rm -T --user "$USER_FLAG" e2e python -m pytest /e2e/tests -p no:cacheprovider -v \
  --junitxml=/results/e2e-junit.xml "$@"
