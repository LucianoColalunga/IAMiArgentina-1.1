# Pruebas de carga

`load_test.py` manda webhooks firmados de WhatsApp, Telegram, Messenger e Instagram contra la
plataforma completa del entorno de punta a punta (con el modelo y las plataformas simulados) y
mide:

- **recepción**: cuánto tardan nginx y la API en contestar 200 al webhook (lo que exige cada
  plataforma);
- **respuesta completa**: desde que sale el webhook hasta que la respuesta llega a la plataforma
  simulada (cola, worker, motor RAG, dispatcher y API de envío);
- mensajes sin respuesta al final y respuestas duplicadas.

Cada mensaje viene de una persona distinta. Un webhook que no recibe 200 se reintenta con espera
creciente, como hacen las plataformas.

## Correrlas

```bash
KEEP=1 scripts/e2e.sh                 # levanta el entorno y lo deja arriba
scripts/load.sh sostenida             # 20 mensajes/s durante 2 minutos
scripts/load.sh escalonada            # 10 -> 25 -> 50 -> 100 -> 150 mensajes/s, 30 s cada uno
scripts/load.sh resiliencia           # 10 mensajes/s reiniciando worker, dispatcher, RabbitMQ,
                                      # Redis y PostgreSQL, y recreando la API
PARALLEL=3 RATE=50 DURATION=60 scripts/load.sh capacidad   # varios generadores en paralelo
```

Resultados en `tests/load/results/` (no se versionan): `<escenario>.json` con percentiles por
escalón, `<escenario>-stats.csv` con CPU y memoria de cada contenedor y, en resiliencia,
`<escenario>-events.log` con la hora de cada reinicio. Las corridas publicadas y su análisis están
en [docs/reports/resultados-pruebas.md](../../docs/reports/resultados-pruebas.md).

Las trazas de OpenTelemetry se apagan durante la carga, porque el colector de prueba es el mismo
simulador y distorsionaría la medición. Para medir con trazas:
`E2E_OTLP_ENDPOINT=http://platforms:9000 scripts/load.sh sostenida`.
