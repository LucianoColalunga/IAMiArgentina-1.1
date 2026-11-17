#!/usr/bin/env bash
# Pruebas de carga, estrés y resiliencia sobre el entorno de punta a punta.
# Requiere el entorno levantado:  KEEP=1 scripts/e2e.sh
#
#   scripts/load.sh sostenida     20 mensajes/s durante 2 minutos
#   scripts/load.sh escalonada    10 -> 25 -> 50 -> 100 -> 150 mensajes/s, 30 s cada escalón
#   scripts/load.sh resiliencia   10 mensajes/s mientras se reinician worker, dispatcher,
#                                 RabbitMQ, Redis y PostgreSQL (uno por vez) y se recrea la API
#                                 (contenedor nuevo, con otra IP, como en una actualización)
#   scripts/load.sh capacidad     varios generadores en paralelo (un solo proceso de Python no
#                                 alcanza para medir más de ~100 mensajes/s sin distorsionar):
#                                 PARALLEL=3 RATE=50 DURATION=60 scripts/load.sh capacidad
#
# Resultados en tests/load/results/<escenario>.json y uso de CPU/memoria en <escenario>-stats.csv.
#
# Las trazas de OpenTelemetry se apagan durante la carga (el colector de prueba es el mismo
# simulador que hace de plataforma y distorsionaría la medición). Para medir con trazas:
#   E2E_OTLP_ENDPOINT=http://platforms:9000 scripts/load.sh sostenida
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export MSYS_NO_PATHCONV=1
PROJECT=iamia-e2e
COMPOSE=(docker compose -p "$PROJECT" --env-file tests/e2e/.env.e2e
  -f docker-compose.yml -f tests/e2e/compose.e2e.yml --profile models)
RESULTS=tests/load/results
mkdir -p "$RESULTS"
SCENARIO="${1:-sostenida}"

export E2E_OTLP_ENDPOINT="${E2E_OTLP_ENDPOINT-}"
# Deja todo el entorno con esta configuración antes de empezar: recrea lo que cambió (también
# migrate e ingest, que son idempotentes) y no toca lo demás. Si quedara algo por recrear, cada
# generador en paralelo intentaría recrearlo a la vez y chocarían.
"${COMPOSE[@]}" up -d --wait --wait-timeout 300 >/dev/null 2>&1

load() {
  "${COMPOSE[@]}" run --rm -T --user "$(id -u):$(id -g)" load --label "$SCENARIO" \
    --out "/results/$SCENARIO.json" --timeline "/results/$SCENARIO-timeline.csv" "$@" > "$RESULTS/$SCENARIO.log"
}

# Muestra de CPU y memoria de cada contenedor cada 5 s mientras dura la prueba.
sample_stats() {
  echo "segundo,contenedor,cpu,memoria" > "$RESULTS/$SCENARIO-stats.csv"
  local start=$SECONDS
  while true; do
    docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' \
      $(docker ps -q --filter "label=com.docker.compose.project=$PROJECT") 2>/dev/null |
      sed "s/^/$((SECONDS - start)),/" >> "$RESULTS/$SCENARIO-stats.csv" || true
    sleep 5
  done
}

restart_later() {  # restart_later <segundos> <servicio>
  sleep "$1"
  echo "$(date +%T) reinicio de $2" >> "$RESULTS/$SCENARIO-events.log"
  "${COMPOSE[@]}" restart "$2" >/dev/null 2>&1
  echo "$(date +%T) $2 de nuevo arriba" >> "$RESULTS/$SCENARIO-events.log"
}

recreate_later() {  # recreate_later <segundos> <servicio>: contenedor nuevo, como al actualizar
  sleep "$1"
  echo "$(date +%T) se recrea $2" >> "$RESULTS/$SCENARIO-events.log"
  "${COMPOSE[@]}" up -d --force-recreate --no-deps --wait "$2" >/dev/null 2>&1
  echo "$(date +%T) $2 recreado y sano" >> "$RESULTS/$SCENARIO-events.log"
}

# Histograma de latencia medido dentro de la API (sin la demora del generador ni de la red).
server_histogram() {
  docker exec "$PROJECT-api-1" python -c "
import urllib.request
text = urllib.request.urlopen('http://127.0.0.1:8000/metrics', timeout=5).read().decode()
for line in text.splitlines():
    if line.startswith('iamia_http_seconds') and 'webhooks' in line:
        print(line)
" 2>/dev/null || true
}
server_histogram > "$RESULTS/$SCENARIO-api-antes.txt"

sample_stats & STATS_PID=$!
trap 'kill $STATS_PID 2>/dev/null || true; server_histogram > "$RESULTS/$SCENARIO-api-despues.txt"' EXIT

status=0
case "$SCENARIO" in
  sostenida)
    load --rate 20 --duration 120 || status=$?
    ;;
  escalonada)
    load --ramp 10,25,50,100,150 --step 30 --drain 300 || status=$?
    ;;
  resiliencia)
    : > "$RESULTS/$SCENARIO-events.log"
    restarts=()
    restart_later 20 worker & restarts+=($!)
    restart_later 45 dispatcher & restarts+=($!)
    restart_later 70 rabbitmq & restarts+=($!)
    restart_later 95 redis & restarts+=($!)
    restart_later 120 postgres & restarts+=($!)
    recreate_later 135 api & restarts+=($!)
    load --rate 10 --duration 150 --drain 300 || status=$?
    wait "${restarts[@]}"
    ;;
  capacidad)
    generators=()
    for i in $(seq 1 "${PARALLEL:-3}"); do
      "${COMPOSE[@]}" run --rm -T --user "$(id -u):$(id -g)" load --label "$SCENARIO-$i" \
        --rate "${RATE:-50}" --duration "${DURATION:-60}" --drain 300 \
        --out "/results/$SCENARIO-$i.json" > "$RESULTS/$SCENARIO-$i.log" &
      generators+=($!)
    done
    for pid in "${generators[@]}"; do wait "$pid" || status=$?; done
    ;;
  *)
    echo "escenario desconocido: $SCENARIO (sostenida, escalonada, resiliencia o capacidad)" >&2
    exit 2
    ;;
esac
echo "resultado: $RESULTS/$SCENARIO.json (código $status: 0 = todo respondido, sin duplicados)"
exit "$status"
