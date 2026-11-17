# Runbooks

Qué hacer cuando salta cada alerta. Cada sección tiene el mismo orden: qué significa, a quién
afecta, cómo confirmar la causa y cómo resolverla. Las alertas están en
`infrastructure/monitoring/prometheus/rules/iamia.rules.yml` y enlazan acá por el ancla de cada
título.

En los ejemplos:

- `dc` es el comando de compose de la instalación, por ejemplo
  `docker compose --env-file .env -f docker-compose.yml -f compose.gpu.yml --profile models`.
- `k` es `kubectl -n iamia`.
- Los logs son JSON, una línea por evento, con `role` (api, worker, dispatcher, maintenance),
  `msg` (nombre del evento) y, cuando aplica, `message_id`, `channel` y `trace_id`. Nunca tienen
  texto de las personas ni direcciones en claro. En Grafana se buscan en Loki con
  `{service="worker"} | json | msg="queue_handler_error"`.

Reglas generales:

- **Primero frenar el daño, después entender.** Reiniciar un rol de la app es seguro: los mensajes
  están guardados en PostgreSQL antes de responderle a la plataforma, las reservas vencen solas y
  el sweeper republica lo pendiente.
- **No borrar colas ni mensajes para "destrabar".** Nada se pierde esperando; algo borrado no se
  recupera.
- Anotar en el canal de guardia qué se vio y qué se hizo, con la hora. Sirve para el post mortem.

## Mapa rápido

| Alerta | Severidad | Sección |
|---|---|---|
| IamiaApiDown | critical | [La API no responde](#api-caida) |
| IamiaApiErrorRate | warning / critical | [Errores 5xx en la API](#api-errores-5xx) |
| IamiaTargetDown | warning | [Prometheus no puede leer un proceso](#target-caido) |
| IamiaEndToEndLatencyHigh | warning | [Respuestas lentas](#latencia-extremo-a-extremo) |
| IamiaNoMessagesProcessed | critical | [Entran mensajes pero no se procesan](#sin-procesamiento) |
| IamiaInboundBacklog | warning / critical | [Crece la cola de entrada](#cola-entrante) |
| IamiaDeadLetters, IamiaDeadLetterQueueGrowing | warning | [Trabajos en la cola de descarte](#cola-de-descarte) |
| IamiaSweeperRequeueSustained | warning | [El sweeper republica sin parar](#sweeper-reencolando) |
| IamiaUndecryptableData | warning | [Datos que no se pueden descifrar](#datos-sin-descifrar) |
| IamiaCircuitOpen | warning | [Circuito de un canal abierto](#circuito-abierto) |
| IamiaLLMCircuitOpen | critical | [Circuito del LLM abierto](#circuito-llm-abierto) |
| IamiaWebhookRejections | warning | [Webhooks rechazados](#webhooks-rechazados) |
| IamiaDeliveryFailures | warning / critical | [Fallan los envíos de un canal](#fallas-de-entrega) |
| IamiaAIBlockedSpike | warning | [Muchas consultas bloqueadas por el motor](#bloqueos-del-motor) |
| IamiaLLMLatencyHigh | warning | [El LLM tarda demasiado](#latencia-llm) |

<a name="api-caida"></a>
## La API no responde

**Qué significa.** Prometheus no puede leer ninguna instancia de la API hace 2 minutos.

**Impacto.** No entran webhooks ni mensajes del chat web, y la consola no carga. Las plataformas
reintentan (Meta hasta 7 días, Telegram durante horas), así que lo que llegue mientras tanto
entra cuando la API vuelve; Messenger desactiva el webhook si falla durante una hora seguida.

**Confirmar.**

```bash
k get pods -l app.kubernetes.io/component=api      # dc ps api
k describe pod <pod-de-api>                         # eventos: OOMKilled, probe fallida, imagen
k logs deploy/api --since=15m | tail -50            # dc logs --since 15m api
```

- `CrashLoopBackOff` con `insecure_configuration` o un error de `Settings` al arrancar: falta o
  está mal una variable. Correr `python -m iamia check-config` con la misma configuración.
- `/health/ready` en 503: alguna dependencia no responde (el cuerpo dice cuál). Ir a la sección
  de esa dependencia.
- Pods en `Pending`: falta capacidad en el cluster o hay un PDB/afinidad que no se puede cumplir.

**Resolver.** Si fue un despliegue, volver a la versión anterior (`k rollout undo deploy/api` o el
rollback de Argo CD). Si es memoria, subir el límite y revisar el tamaño de los cuerpos que
entran. Confirmar después que `iamia_messages_received_total` vuelve a crecer.

<a name="api-errores-5xx"></a>
## Errores 5xx en la API

**Qué significa.** Más del 2 % (warning) o del 10 % (critical) de los pedidos terminan en 5xx.

**Impacto.** Las plataformas reintentan los webhooks que reciben 5xx, así que en general no se
pierde nada, pero con un 10 % sostenido Meta puede marcar el webhook como inestable.

**Confirmar.** Separar los 503 esperables de los 500:

```bash
k logs deploy/api --since=15m | grep -E '"msg": "(dependency_unavailable|unhandled_error)"' | tail
```

- `dependency_unavailable`: PostgreSQL o Redis no responden (el campo `error` dice qué excepción).
  La API contesta 503 con `Retry-After: 5` a propósito. Revisar la dependencia: estado del
  Cluster de CloudNativePG (`k get cluster iamia-db`), del pooler y de Redis.
- `unhandled_error`: un error del programa. Buscar el `request_id` en la traza y correlacionar con
  el último despliegue.
- 503 del chat web con `BUSY_MESSAGE`: el LLM está saturado (ver [El LLM tarda demasiado](#latencia-llm)).

**Resolver.** Si es una dependencia, ver su estado y su almacenamiento (disco lleno en PostgreSQL
es la causa más común). Si es un error nuevo, volver atrás el despliegue.

<a name="target-caido"></a>
## Prometheus no puede leer un proceso

**Qué significa.** Un target (`api`, `worker`, `dispatcher`, `maintenance`, `rabbitmq`) no
responde al scrape hace 5 minutos.

**Confirmar.** Si el pod está `Running` y el resto de las alertas está tranquilo, suele ser red:
una NetworkPolicy que no deja pasar a Prometheus (namespace `monitoring`) al puerto 9100 de los
workers o al 8000 de la API.

```bash
k get pods -o wide | grep <rol>
k exec deploy/<rol> -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/readyz').status)"
```

**Resolver.** Si el proceso está caído, ver [La API no responde](#api-caida) (mismas causas). Si
es red, revisar `infrastructure/kubernetes/base/networkpolicies/monitoring.yaml` y los labels del
namespace de Prometheus.

<a name="latencia-extremo-a-extremo"></a>
## Respuestas lentas

**Qué significa.** El p95 entre que entra un mensaje y sale su respuesta supera 30 s en un canal
durante 10 minutos. Incluye las conversaciones derivadas a operadores, que tardan lo que tarda la
persona en contestar: si el aumento coincide con muchas derivaciones, no hay falla técnica.

**Confirmar,** en este orden:

1. Cola de entrada (`iamia.inbound`) creciendo: faltan workers o el LLM no da abasto.
2. `iamia_llm_seconds` alto: ver [El LLM tarda demasiado](#latencia-llm).
3. Throttle del canal: X tiene 15 mensajes cada 15 minutos para toda la cuenta; con más demanda
   las respuestas esperan en `iamia.outbound.x` y la latencia sube sin que nada falle.
4. Circuito del canal abierto o reintentos por 429 de la plataforma (`iamia_deliveries_total`
   con `result="rate_limited"`).

**Resolver.** Escalar lo que esté al límite (workers con KEDA, réplicas de `llm`). Si el cuello es
el throttle de un canal, avisar a comunicación: es un límite de la plataforma, no del sistema.

<a name="sin-procesamiento"></a>
## Entran mensajes pero no se procesan

**Qué significa.** La API recibió mensajes en los últimos 15 minutos y los workers no terminaron
ninguno. Es la alerta más grave: la gente escribe y nadie contesta.

**Confirmar.**

```bash
k get pods -l app.kubernetes.io/component=worker
k logs deploy/worker --since=15m | grep -vE '"level": "INFO"' | tail -30
```

- Workers caídos o en crash: ver los eventos del pod.
- Workers vivos con `queue_handler_transient_error` en todos los mensajes: PostgreSQL o Redis no
  responden. Los trabajos vuelven solos con espera (1 s, 5 s, 30 s, 2 min, 10 min); cuando la
  dependencia vuelva se procesan sin intervención.
- Workers vivos sin errores y la cola llena: el consumidor perdió la conexión con RabbitMQ y no
  se recuperó. Reiniciar los workers (`k rollout restart deploy/worker`).
- `llm_circuit_open` en los bloqueos del motor: ver [Circuito del LLM abierto](#circuito-llm-abierto).

**Resolver.** Reiniciar los workers es seguro (los mensajes en curso vuelven a la cola y las
reservas vencen solas en 3 minutos). Si la causa es una dependencia, resolverla primero.

<a name="cola-entrante"></a>
## Crece la cola de entrada

**Qué significa.** `iamia.inbound` tiene más de 1000 mensajes listos y crece hace 15 minutos
(warning), o pasó los 10.000 (critical).

**Confirmar.** Comparar lo que entra con lo que se procesa en el tablero "IAMiArgentina - Visión
general" (paneles "Mensajes recibidos por minuto" y "Cola de entrada en espera"). Mirar si KEDA
ya está en el máximo de workers:

```bash
k get scaledobject worker -o jsonpath='{.status}' ; k get hpa
k exec iamia-rabbitmq-server-0 -- rabbitmqctl list_queues -p iamia name messages_ready messages_unacknowledged
```

**Resolver.**

- Si KEDA está en el máximo y el LLM tiene margen: subir `maxReplicaCount` del ScaledObject.
- Si el LLM está saturado (latencia alta, `llm_busy` en los bloqueos): sumar réplicas de `llm`
  (una por GPU libre). Más workers sin más GPU solo arman cola adentro de vLLM.
- Un pico por una novedad (un trámite nuevo, una caída de otro servicio) se absorbe con la caché:
  la primera respuesta se genera una vez y el resto sale de Redis. Revisar
  `iamia_ai_cache_hits_total`: si es bajo, las preguntas son muy distintas entre sí.
- Con más de 500.000 mensajes la cola llega a `x-max-length` y rechaza publicaciones. No se pierde
  nada (queda en PostgreSQL y lo republica el sweeper), pero conviene no llegar ahí.

<a name="cola-de-descarte"></a>
## Trabajos en la cola de descarte

**Qué significa.** Se mandaron trabajos a `iamia.dead.inbound` o `iamia.dead.outbound`: agotaron
sus intentos (envíos que fallan 8 veces, errores del programa que se repiten 5 veces con espera),
llegaron inválidos o superaron `x-delivery-limit`.

**Confirmar.** Los envíos fallidos se ven en la consola, sección "Problemas", con el código de
error de la plataforma. Para el resto:

```bash
k logs deploy/worker --since=1h | grep '"msg": "queue_handler_error"' | tail
k logs deploy/dispatcher-general --since=1h | grep '"msg": "delivery_failed"' | tail
```

**Resolver.**

- Envíos: corregir la causa (credencial vencida, número inválido, ventana de 24 h cerrada) y usar
  "Reintentar" en la consola. Queda auditado.
- Errores del programa: es un bug. El mensaje sigue guardado en PostgreSQL; una vez corregido,
  marcarlo para republicar:

  ```sql
  -- mensajes de entrada que quedaron sin procesar
  UPDATE messages SET enqueued_at = NULL
   WHERE direction = 'in' AND status = 'received' AND created_at > now() - interval '1 day';
  ```

  El sweeper los republica en su siguiente vuelta (30 s). La DLQ se puede vaciar después de
  revisarla; los trabajos solo tienen ids, no contenido.

<a name="sweeper-reencolando"></a>
## El sweeper republica sin parar

**Qué significa.** Hace más de 30 minutos que el sweeper encuentra mensajes guardados que no
llegaron a RabbitMQ. Falla la publicación: la cola está llena, el broker activó una alarma de
memoria o disco, o la conexión es inestable.

**Confirmar.**

```bash
k exec iamia-rabbitmq-server-0 -- rabbitmq-diagnostics alarms
k exec iamia-rabbitmq-server-0 -- rabbitmq-diagnostics check_local_alarms
k logs deploy/maintenance --since=30m | grep sweep_done | tail -5
```

**Resolver.** Con alarma de disco, ampliar el volumen del broker; con alarma de memoria, ver qué
cola acumula (casi siempre la de un canal con el circuito abierto mucho tiempo). Mientras tanto no
hay pérdida: la API sigue aceptando mensajes y todo queda en PostgreSQL.

<a name="datos-sin-descifrar"></a>
## Datos que no se pueden descifrar

**Qué significa.** Después de una rotación de `DATA_ENCRYPTION_KEYS`, el mantenimiento recifra lo
guardado con las claves anteriores y encontró datos que no puede leer: están cifrados con una
clave que ya no está en la configuración (se retiró antes de tiempo) o fueron modificados en la
base. Una respuesta a esas conversaciones falla hasta que se resuelva.

**Confirmar.**

```bash
k logs deploy/maintenance --since=2h | grep key_rotation
k exec deploy/maintenance -- python -m iamia key-usage
```

`key-usage` muestra, por tabla, cuántos datos usa cada clave. La que no está en la configuración
es la que falta.

**Resolver.** Si se retiró una clave que todavía se usaba, volver a agregarla al final de
`DATA_ENCRYPTION_KEYS` en el gestor de secretos y desplegar: la próxima vuelta del mantenimiento
recifra esos datos. Si la clave se perdió, esos datos no se recuperan; las direcciones se arreglan
solas cuando la persona vuelve a escribir. Si nadie cambió claves, tratarlo como incidente de
seguridad: alguien modificó la base.

<a name="circuito-abierto"></a>
## Circuito de un canal abierto

**Qué significa.** Tras varias fallas seguidas al enviar por un canal, el dispatcher cortó los
envíos de ese canal (circuit breaker) para no insistir contra una plataforma caída. Los envíos se
reprograman y el circuito prueba de nuevo cada 30 s. Los demás canales no se ven afectados: cada
uno tiene su cola y su circuito.

**Confirmar.** El tipo de falla en `iamia_deliveries_total{channel="..."}`:

- `auth`: token vencido o revocado. Es lo más común en Meta (tokens de System User) y en X
  (refresh token). No se resuelve solo.
- `retryable` o `rate_limited`: la plataforma está caída o limitando. Ver su página de estado.

**Resolver.** Con `auth`, renovar la credencial en el gestor de secretos (Vault → External
Secrets) y reiniciar el dispatcher de ese canal. En X, si se perdió el refresh token, repetir la
autorización inicial con `python -m iamia x-authorize`. Cuando el circuito se cierra, los envíos
pendientes salen solos en orden.

<a name="circuito-llm-abierto"></a>
## Circuito del LLM abierto

**Qué significa.** Los workers no pueden consultar al modelo: varias fallas o timeouts seguidos.
Los mensajes no se pierden: se reintentan con espera y, si el modelo no vuelve después de 4
intentos, la persona recibe un aviso de servicio no disponible.

**Confirmar.**

```bash
k get pods -l app.kubernetes.io/name=vllm -o wide
k logs deploy/llm --since=15m | tail -50     # CUDA out of memory, error al cargar el modelo
nvidia-smi                                   # en el nodo: GPU visibles, memoria, Xid en dmesg
```

**Resolver.** Un pod de vLLM que no arranca por falta de memoria de GPU suele ser un cambio de
modelo o de `--max-model-len`; volver al valor anterior. Con una GPU con errores Xid, sacar el
nodo de servicio (`kubectl cordon`) y avisar al proveedor del hardware. Mientras el LLM está
caído, las preguntas repetidas se siguen respondiendo desde la caché.

<a name="webhooks-rechazados"></a>
## Webhooks rechazados

**Qué significa.** Más de 12 webhooks por minuto rechazados en un canal. El label `reason` dice
por qué:

- `signature`: la firma no coincide. O alguien intenta mandar webhooks falsos, o se rotó el
  secreto de la app en la plataforma y no se actualizó acá.
- `challenge`: falla la verificación de suscripción (verify token distinto).
- `payload`: el cuerpo no tiene el formato esperado (cambio de versión de la API de la plataforma).

**Confirmar.** Si todos los webhooks legítimos del canal fallan, la cantidad de mensajes recibidos
de ese canal cae a cero: es un secreto rotado. Si los mensajes legítimos siguen entrando y además
hay rechazos, es tráfico falso: no requiere acción más allá de vigilar el volumen (nginx limita
el resto).

**Resolver.** Actualizar el secreto (`*_APP_SECRET`, `TELEGRAM_WEBHOOK_SECRET`, `X_WEBHOOK_SECRET`,
`TWILIO_AUTH_TOKEN`, `EMAIL_INBOUND_SECRET`) y reiniciar la API. Con `payload`, revisar las notas
de versión de la plataforma y fijar la versión de la API que se usa (`META_GRAPH_API_VERSION`).

<a name="fallas-de-entrega"></a>
## Fallan los envíos de un canal

**Qué significa.** Más del 5 % (warning) o del 25 % (critical) de los intentos de envío por un
canal fallan en 15 minutos.

**Confirmar** por tipo de falla (`result` en `iamia_deliveries_total`):

| result | Causa habitual | Acción |
|---|---|---|
| `auth` | credencial vencida | renovarla (ver [Circuito de un canal abierto](#circuito-abierto)) |
| `permanent` | destinatario inválido, contenido rechazado | se ven en la consola; revisar el código |
| `window_closed` | WhatsApp, Messenger o Instagram fuera de la ventana de 24 h | configurar la plantilla de recontacto (WhatsApp) o responder desde la consola dentro de los 7 días (Messenger e Instagram, etiqueta HUMAN_AGENT) |
| `rate_limited` | límite de la plataforma | bajar el límite propio del canal (`CHANNEL_RATE_LIMITS`) |
| `retryable` | plataforma con errores | se reintenta solo; ver su página de estado |

**Resolver.** Según la tabla. Los envíos que agotaron sus intentos quedan en la consola para
reintentarlos cuando la causa esté corregida.

<a name="bloqueos-del-motor"></a>
## Muchas consultas bloqueadas por el motor

**Qué significa.** Más del 10 % de las consultas termina bloqueada por un mismo motivo (`reason`):

- `prompt_injection`: intentos de manipular al asistente. Puede ser una campaña (mismo texto desde
  muchos contactos). El bloqueo funciona: no se llama al modelo. Vigilar el volumen y, si viene
  de pocos orígenes, bajar los límites por contacto.
- `out_of_scope` o `low_grounding`: preguntas sobre algo que la base de conocimiento no cubre.
  Suele pasar cuando hay una novedad (un trámite nuevo) que todavía no está en las FAQ: avisar a
  quien mantiene el contenido y reindexar (`python -m iamia ingest`).
- `llm_busy` o `llm_circuit_open`: falta capacidad de LLM (ver [El LLM tarda demasiado](#latencia-llm)).
- `token_budget`: un mismo cliente agotó su cupo diario de tokens. Si es uno solo, es abuso o un
  bucle de una integración; si son muchos, el cupo quedó corto.
- `canary_leak` o `prompt_leak`: el control de salida descartó respuestas que filtraban el prompt.
  Revisar los logs `output_guard_triggered` y la versión del prompt; si coincide con un cambio de
  modelo, volver atrás.

<a name="latencia-llm"></a>
## El LLM tarda demasiado

**Qué significa.** El p95 de generación supera los 20 s durante 10 minutos.

**Confirmar.** En las métricas propias de vLLM (Prometheus las recolecta con el ServiceMonitor
`vllm`): pedidos en espera (`vllm:num_requests_waiting`), uso de la KV cache
(`vllm:kv_cache_usage_perc`; en versiones viejas, `vllm:gpu_cache_usage_perc`) y tokens por
segundo. Si la espera crece con la KV cache cerca del
100 %, falta capacidad.

**Resolver.**

- Sumar réplicas de `llm` si hay GPU libres.
- Bajar `MAX_CONCURRENT_LLM_CALLS` por worker para que la espera sea en la cola (donde se ve y se
  mide) y no adentro de vLLM.
- Revisar que la caché esté funcionando (`iamia_ai_cache_hits_total`): una caída de Redis hace
  que todas las preguntas lleguen al modelo.
- Si la latencia subió después de cambiar de modelo o de versión de vLLM, volver atrás.
