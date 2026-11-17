# Observabilidad de IAMiArgentina

Configuración de métricas, alertas, dashboards, logs y trazas. Sirve tal cual para el stack de
docker compose; en Kubernetes se reutilizan las reglas, los dashboards y el pipeline de logs (ver
más abajo).

```
prometheus/prometheus.yml               scrape de los servicios de compose
prometheus/rules/iamia.rules.yml        recording rules y alertas (fuente de verdad)
alertmanager/alertmanager.yml           ruteo, agrupación e inhibiciones
grafana/provisioning/datasources/       Prometheus, Loki y Tempo con uid fijos
grafana/provisioning/dashboards/        proveedor que carga grafana/dashboards
grafana/dashboards/                     "Visión general", "Motor de IA" e "Infraestructura y GPU"
loki/loki-config.yml                    Loki monolítico, 30 días de logs
alloy/config.alloy                      logs de los contenedores hacia Loki
otel/otel-collector.yml                 OTLP hacia Tempo, más span metrics
tempo/tempo.yml                         Tempo monolítico, 7 días de trazas
```

## Docker compose

`compose.observability.yml`, en la raíz del repositorio, levanta todo esto junto con la
aplicación:

```bash
cp .env.observability.example .env.observability   # contraseña de Grafana, destinos de alertas
docker compose --env-file .env --env-file .env.observability \
  -f docker-compose.yml -f compose.observability.yml up -d
```

Los servicios se llaman como en la tabla (las configuraciones se refieren a ellos por nombre) y
las rutas son relativas a la raíz, que es el directorio del proyecto de compose.

| Servicio | Imagen | Montajes (solo lectura) | Puertos internos |
|---|---|---|---|
| `prometheus` | `prom/prometheus` | `prometheus/prometheus.yml` en `/etc/prometheus/prometheus.yml`, `prometheus/rules` en `/etc/prometheus/rules` | 9090 |
| `alertmanager` | `prom/alertmanager` | `alertmanager/alertmanager.yml` en `/etc/alertmanager/alertmanager.yml` | 9093 |
| `grafana` | `grafana/grafana` | `grafana/provisioning` en `/etc/grafana/provisioning`, `grafana/dashboards` en `/var/lib/grafana/dashboards` | 3000 |
| `loki` | `grafana/loki` (3.x) | `loki/loki-config.yml` en `/etc/loki/config.yml`; volumen en `/loki` | 3100 |
| `alloy` | `grafana/alloy` | `alloy/config.alloy` en `/etc/alloy/config.alloy`, `/var/run/docker.sock` | 12345 |
| `otel-collector` | `otel/opentelemetry-collector-contrib` | `otel/otel-collector.yml` en `/etc/otelcol-contrib/config.yaml` | 4317, 4318, 8889, 13133 |
| `tempo` | `grafana/tempo` (2.x) | `tempo/tempo.yml` en `/etc/tempo/tempo.yml`; volumen en `/var/tempo` | 3200, 4317 |

- Fijar versiones (y digest) de todas las imágenes, igual que en el resto del stack.
- Comandos: Loki y Tempo con `-config.file=<ruta>`; Alloy con
  `run /etc/alloy/config.alloy --storage.path=/var/lib/alloy/data`.
- Alertmanager lee sus credenciales de archivos: `smtp_password` y `guardia_webhook_url` en
  `/etc/alertmanager/secrets/`, con `secrets:` de compose (`target` acepta una ruta absoluta).
  Nunca en el YAML.
- El socket de Docker equivale a root en el host: montarlo solo en Alloy, en solo lectura, y no
  publicar el puerto de Alloy.
- Grafana no se publica directo: por el nginx del borde con autenticación, o en
  `127.0.0.1:3000` para uso local.

Nombres de job de Prometheus: `prometheus`, `api`, `worker`, `dispatcher`, `maintenance`,
`rabbitmq` y `rabbitmq-detailed`. Son los mismos que en Kubernetes, así que las reglas y los
dashboards sirven en los dos entornos. Los roles se descubren por DNS: con
`docker compose up --scale worker=3` se leen las tres réplicas.

## Kubernetes

Con kube-prometheus-stack, la base de `../kubernetes` ya trae:

- `monitoring/prometheusrule.yaml`: copia de `prometheus/rules/iamia.rules.yml` dentro de un
  `PrometheusRule`. El archivo de reglas es la fuente de verdad: al cambiarlo, copiar los grupos a
  `spec.groups`. Un chequeo para CI con yq:

  ```bash
  diff <(yq '.groups' infrastructure/monitoring/prometheus/rules/iamia.rules.yml) \
       <(yq '.spec.groups' infrastructure/kubernetes/base/monitoring/prometheusrule.yaml)
  ```

- ServiceMonitor y PodMonitor con la etiqueta `release: kube-prometheus-stack`. Usan
  `jobLabel: app.kubernetes.io/component` para que los jobs se llamen `api`, `worker`,
  `dispatcher` y `maintenance`, y el endpoint detallado de RabbitMQ se reetiqueta como
  `rabbitmq-detailed`. También se scrapean CloudNativePG, PgBouncer, vLLM y oauth2-proxy.

Dashboards: el sidecar de Grafana de kube-prometheus-stack carga los ConfigMap con la etiqueta
`grafana_dashboard: "1"`.

```bash
kubectl -n monitoring create configmap iamia-dashboards \
  --from-file=infrastructure/monitoring/grafana/dashboards/ \
  --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
  | kubectl apply --server-side -f -
```

Los dashboards eligen el datasource con la variable `datasource`, así que no dependen del uid que
tenga Prometheus en cada Grafana.

| Dashboard | Qué muestra | De dónde salen los datos |
|---|---|---|
| Visión general | mensajes por canal, latencia de punta a punta, entregas, colas, DLQ, circuit breakers, errores HTTP | la aplicación y RabbitMQ (los dos entornos) |
| Motor de IA | consultas, aciertos de caché por capa, bloqueos, latencia y tokens del LLM, puntajes de recuperación y anclaje | la aplicación (los dos entornos) |
| Infraestructura y GPU | CPU y memoria por rol y por contenedor; uso, memoria, temperatura y potencia de cada GPU; cola, caché de atención, tokens/s y tiempo al primer token de vLLM | procesos de la aplicación (los dos entornos); en Kubernetes además cAdvisor, `dcgm-exporter` del NVIDIA GPU Operator (con su ServiceMonitor habilitado) y vLLM |

Alertmanager: kube-prometheus-stack trae el suyo (`alertmanager.config` en los values o
`AlertmanagerConfig`). `alertmanager/alertmanager.yml` es la referencia de rutas e inhibiciones.

Loki, Alloy y Tempo: con los charts oficiales. El pipeline de `config.alloy` (JSON, labels y
structured metadata) es el mismo; cambia la fuente (`discovery.kubernetes` y
`loki.source.kubernetes`) y los labels de origen (`namespace`, `pod`, `container`). Si vLLM manda
trazas a un collector dentro del cluster, hace falta una NetworkPolicy de salida hacia el puerto
4317 del collector (el namespace `iamia` es default deny).

## Labels y cardinalidad

- Métricas: las etiquetas de la app son acotadas (canal, resultado, cola, plantilla de ruta,
  motivo). Nunca por persona, mensaje ni conversación.
- Logs: labels `service`, `container`, `level`, `role` y `channel`. `request_id`, `trace_id` y
  `message_id` van como structured metadata: se filtran igual y no crean una serie por mensaje.
  `level` llega en mayúsculas, como lo escribe Python (`INFO`, `WARNING`, `ERROR`).
- Los logs no traen el texto de los mensajes ni datos de contacto (`backend/iamia/core/logs.py`).

Consultas útiles en Loki:

```logql
{service="worker", level="ERROR"}
{role="api"} | request_id="<id>"
{service=~"api|worker|dispatcher"} | trace_id="<id>"
sum by (role, level) (count_over_time({role=~".+"}[5m]))
```

Un mensaje que entra por webhook se sigue de punta a punta: la API registra su `request_id` y los
workers y dispatchers usan ese mismo valor como `trace_id`.

Trazas: con `OTEL_EXPORTER_OTLP_ENDPOINT` definido (`compose.observability.yml` lo apunta al
collector) cada rol exporta sus spans por OTLP/HTTP; sin esa variable no se exporta nada. El id de
traza es el mismo `trace_id` de los logs (formato W3C, 32 hex), así que la API, el worker y el
dispatcher suman sus spans a una sola traza por mensaje y Grafana salta de una línea de Loki a la
traza en Tempo ([ADR 0008](../../docs/architecture/decisiones/0008-trazas-con-el-id-del-mensaje.md)).
`OTEL_TRACES_SAMPLE_RATIO` regula qué parte de los mensajes se traza. vLLM puede mandar las suyas
al mismo collector (`--otlp-traces-endpoint`).

## Retención

| Señal | Retención | Dónde se configura |
|---|---|---|
| Métricas | 15 días (valor por defecto) | `--storage.tsdb.retention.time` de Prometheus |
| Logs | 30 días | `loki/loki-config.yml` (`retention_period: 720h`) |
| Trazas | 7 días | `tempo/tempo.yml` (`block_retention: 168h`) |

## Alertas

Las critical van a la guardia (correo y webhook, se repiten cada hora); las warning, al equipo de
plataforma (cada 12 horas). Una critical silencia a su warning y, con el circuito del LLM
abierto, no se avisan la latencia del LLM ni los bloqueos del motor.

| Alerta | Severidad | Ancla del runbook |
|---|---|---|
| IamiaApiErrorRate | warning / critical | `api-errores-5xx` |
| IamiaApiDown | critical | `api-caida` |
| IamiaTargetDown | warning | `target-caido` |
| IamiaEndToEndLatencyHigh | warning | `latencia-extremo-a-extremo` |
| IamiaNoMessagesProcessed | critical | `sin-procesamiento` |
| IamiaInboundBacklog | warning / critical | `cola-entrante` |
| IamiaDeadLetters, IamiaDeadLetterQueueGrowing | warning | `cola-de-descarte` |
| IamiaSweeperRequeueSustained | warning | `sweeper-reencolando` |
| IamiaUndecryptableData | warning | `datos-sin-descifrar` |
| IamiaCircuitOpen | warning | `circuito-abierto` |
| IamiaLLMCircuitOpen | critical | `circuito-llm-abierto` |
| IamiaWebhookRejections | warning | `webhooks-rechazados` |
| IamiaDeliveryFailures | warning / critical | `fallas-de-entrega` |
| IamiaAIBlockedSpike | warning | `bloqueos-del-motor` |
| IamiaLLMLatencyHigh | warning | `latencia-llm` |

`runbook_url` apunta a `docs/manual/runbooks.md#<ancla>` en el repositorio del proyecto. Cada
ancla de la tabla es un título de ese archivo; si se mueve el repositorio, actualizar la URL en
`prometheus/rules/iamia.rules.yml` y en `infrastructure/kubernetes/base/monitoring/prometheusrule.yaml`.

## Validación

```bash
cd infrastructure/monitoring
promtool check rules prometheus/rules/iamia.rules.yml
promtool check config prometheus/prometheus.yml    # dentro del contenedor, con las reglas montadas
amtool check-config alertmanager/alertmanager.yml
loki -config.file=loki/loki-config.yml -verify-config
alloy fmt alloy/config.alloy
otelcol-contrib validate --config=otel/otel-collector.yml
for f in grafana/dashboards/*.json; do jq empty "$f"; done
```

Sin los binarios instalados, las mismas herramientas corren desde sus imágenes; por ejemplo:

```bash
docker run --rm -v "$PWD/prometheus:/p:ro" --entrypoint promtool prom/prometheus \
  check rules /p/rules/iamia.rules.yml
```
