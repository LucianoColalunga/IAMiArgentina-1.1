# Administración

Este capítulo reúne las tareas de todos los días: prender y apagar, mirar logs y métricas,
detectar errores, atender desde la consola, actualizar la base de conocimiento, los modelos y la
aplicación, y sumar un canal nuevo. Los ejemplos usan Docker Compose; al final de cada sección
está el equivalente en Kubernetes cuando cambia.

## Iniciar y detener

| Acción | Docker Compose | Kubernetes |
|---|---|---|
| Iniciar todo | `docker compose up -d` | `kubectl apply` del manifiesto (o sincronizar en Argo CD) |
| Ver el estado | `docker compose ps` | `kubectl -n iamia get pods` |
| Detener sin borrar nada | `docker compose stop` | `kubectl -n iamia scale deployment --all --replicas=0` |
| Volver a iniciar | `docker compose start` | restaurar las réplicas (o volver a aplicar) |
| Reiniciar un rol | `docker compose restart worker` | `kubectl -n iamia rollout restart deployment/worker` |
| Más réplicas de un rol | `docker compose up -d --scale worker=3` | automático con HPA y KEDA, o `kubectl scale` |
| Apagar y quitar los contenedores | `docker compose down` (los datos quedan) | `kubectl delete` de los recursos de la app |

> `docker compose down -v` borra también los volúmenes: se pierde la base de datos. Usarlo solo en
> un entorno de prueba.

Los roles se pueden detener y arrancar en cualquier orden sin perder mensajes: lo que llega
mientras el worker o el dispatcher están detenidos queda en la base y en las colas, y se procesa
cuando vuelven.

## Logs

Cada proceso escribe una línea JSON por evento en la salida estándar, con la fecha (`ts`), el
nivel, el rol, el mensaje y el contexto: `trace_id`, el id del mensaje y el canal cuando
corresponde. Los logs nunca llevan el texto de los mensajes ni las direcciones de las personas.

```bash
docker compose logs -f api worker            # seguir los logs de dos roles
docker compose logs --since 1h dispatcher    # la última hora de un rol
kubectl -n iamia logs deployment/worker -f   # en Kubernetes
```

Con la observabilidad levantada, los logs llegan a Loki (30 días) y se consultan desde Grafana.
Algunas consultas útiles:

```text
{service="worker"} | json | level="ERROR"
{service=~"api|worker|dispatcher"} | json | trace_id="<id de la traza>"
{service="dispatcher"} | json | msg="delivery_failed"
```

Para seguir un mensaje de punta a punta alcanza con su `trace_id`: es el mismo en los logs de la
API, el worker y el dispatcher, y desde una línea de Loki se salta a la traza completa en Tempo.

## Métricas y tableros

Cada rol publica sus métricas en `/metrics` (cerrado al exterior) y Prometheus las lee cada pocos
segundos. Grafana tiene tres tableros, que sirven igual con Compose y con Kubernetes:

| Tablero | Para qué | Paneles principales |
|---|---|---|
| Visión general | ver de un vistazo si el servicio funciona | mensajes por minuto y por canal, errores 5xx, cola de entrada, circuitos abiertos, latencia de punta a punta, entregas, derivaciones a personas, webhooks rechazados |
| Motor de IA | calidad y costo de las respuestas | consultas por resultado, aciertos de caché por capa, consultas bloqueadas por motivo, latencia del modelo, tokens por segundo, puntajes de recuperación y de anclaje |
| Infraestructura y GPU | capacidad | CPU y memoria por rol, uso, memoria, temperatura y potencia de cada GPU, pedidos en curso en vLLM, caché de atención, tiempo hasta el primer token |

Métricas clave para mirar primero:

| Métrica | Qué indica |
|---|---|
| `iamia_messages_received_total` | mensajes que entran, por canal |
| `iamia_end_to_end_seconds` | tiempo desde que llega el mensaje hasta que sale la respuesta |
| `iamia_webhook_rejected_total` | webhooks rechazados por firma o formato (un salto puede ser un ataque o una credencial cambiada) |
| `iamia_ai_cache_hits_total` | respuestas que salieron de la caché, por capa |
| `iamia_llm_tokens_total` | tokens consumidos por el modelo |
| `iamia_key_rotation_rows_total` | avance del recifrado durante una rotación de claves |

## Alertas

Prometheus evalúa 16 alertas y Alertmanager las agrupa y las manda al canal de guardia. Cada una
tiene su procedimiento en `docs/manual/runbooks.md`:

| Alerta | Qué avisa |
|---|---|
| `IamiaApiDown` | la API no responde |
| `IamiaApiErrorRate` | demasiados errores 5xx |
| `IamiaTargetDown` | Prometheus no puede leer un proceso |
| `IamiaEndToEndLatencyHigh` | las respuestas tardan más de lo esperado |
| `IamiaNoMessagesProcessed` | entran mensajes pero no se procesan |
| `IamiaInboundBacklog` | crece la cola de entrada |
| `IamiaDeadLetters`, `IamiaDeadLetterQueueGrowing` | hay trabajos en la cola de descarte |
| `IamiaSweeperRequeueSustained` | el sweeper republica sin parar |
| `IamiaUndecryptableData` | hay datos que no se pueden descifrar (falta una clave) |
| `IamiaCircuitOpen` | se abrió el circuito de un canal |
| `IamiaLLMCircuitOpen` | se abrió el circuito del modelo |
| `IamiaWebhookRejections` | se rechazan webhooks |
| `IamiaDeliveryFailures` | fallan los envíos de un canal |
| `IamiaAIBlockedSpike` | muchas consultas bloqueadas por el motor |
| `IamiaLLMLatencyHigh` | el modelo tarda demasiado |

## Detectar y resolver errores

1. **La alerta** dice qué pasa y en qué rol o canal, con el enlace a su runbook.
2. **El tablero** muestra desde cuándo y cuánto: si es un canal o todos, si coincide con un
   despliegue, si el modelo está lento.
3. **Los logs** del rol, filtrados por nivel o por `trace_id`, muestran la causa concreta.
4. **La consola**, en Problemas, lista los envíos que fallaron con el error de la plataforma. Un
   administrador los reintenta cuando la causa está resuelta.

Los errores transitorios se reintentan solos, con esperas crecientes (de 1 segundo a 30
minutos). Los envíos que la plataforma rechaza de forma definitiva y los que agotan los
reintentos quedan en Problemas, en la consola; los segundos, además, en la cola de descarte.

## Atender desde la consola

La consola (`/console/`) es la herramienta de las personas que atienden. Las conversaciones se
ven en cuatro pestañas:

| Pestaña | Qué muestra |
|---|---|
| Esperando persona | las que el asistente derivó; se atienden primero, por orden de llegada |
| Con operador/a | las que alguien ya tomó |
| Asistente | las que está respondiendo el asistente; se pueden mirar sin intervenir |
| Cerradas | las terminadas o vencidas |

Para atender: abrir la conversación, **Tomar**, leer el historial (los datos personales aparecen
como `[DNI]`, `[EMAIL]`, ...), responder y, al terminar, **Devolver al asistente** o **Cerrar**.
La respuesta sale por el mismo canal por el que escribió la persona. Si nadie toma una
conversación en 4 horas, el sistema le avisa a la persona que puede seguir con el asistente o
volver a pedir atención más tarde.

## Actualizar la base de conocimiento

1. Editar o agregar las FAQs en `data/faqs` (JSON o guías en Markdown), o sumar páginas oficiales
   a `data/sources.txt`.
2. Revisar qué se va a indexar: `dc run --rm ingest ingest --dry-run`.
3. Indexar: `dc run --rm ingest ingest --if-changed` (o `--from-web` para las páginas).
4. La versión nueva se valida y se activa sola; los procesos la toman en 30 segundos.
5. Si algo salió mal, volver a la anterior: `dc run --rm ingest ingest --rollback`.

En Kubernetes la ingesta corre como CronJob todos los domingos; para correrla en el momento:
`kubectl -n iamia create job --from=cronjob/ingest ingest-manual`.

## Actualizar los modelos

1. Elegir el modelo y verificar que entre en la GPU con margen para la caché de atención.
2. Precargar los pesos en el volumen de modelos (en Kubernetes, el Job `vllm-model-download`).
3. Cambiar `LLM_MODEL` (y, si hace falta, los parámetros de vLLM) y desplegar.
4. Medir con las preguntas de control y con `vllm bench serve` antes de abrir todo el tráfico.
5. Si cambió el modelo de embeddings, volver a indexar la base para que la versión nueva lo use.

Las cachés no mezclan respuestas de modelos distintos: el modelo forma parte de la clave.

## Actualizar la aplicación

```bash
git pull                    # o checkout del tag de la versión nueva
docker compose build
docker compose up -d        # migrate corre primero; los roles arrancan cuando terminó
```

Las migraciones son compatibles con la versión anterior, así que volver atrás es construir y
levantar la imagen anterior (`git checkout v1.1.0`). En Kubernetes, el pipeline corre la
migración como Job y después actualiza cada rol de forma gradual.

## Rotar claves y credenciales

- **Clave de cifrado:** generarla con `gen-secrets`, agregarla primero en
  `DATA_ENCRYPTION_KEYS`, desplegar, esperar a que el mantenimiento termine de recifrar y retirar
  la anterior cuando `python -m iamia key-usage` muestre que nada la usa (capítulo 6).
- **Credenciales de un canal:** cambiarlas en la plataforma y en `.env` o en el gestor de
  secretos; reiniciar la API (secretos de firma) o el dispatcher (tokens de envío).
- **Acceso a la consola:** se da y se quita en el proveedor de identidad, asignando o quitando
  roles. No hay usuarios propios que administrar.

## Agregar una integración nueva

La arquitectura de adaptadores (capítulo 3) hace que sumar un canal no toque el núcleo. Los pasos:

1. **Registrar el canal** en el enum `Channel` (`channels/models.py`). Con eso la aplicación
   crea su cola de salida (`iamia.outbound.<canal>`) al arrancar.
2. **Escribir el adaptador** en `integrations/<canal>/adapter.py`: un verificador de webhooks
   (firma y desafío), un parser que convierte el pedido de la plataforma en `InboundMessage`, un
   formatter que adapta el texto a los límites del canal y un sender que lo envía. Cada pieza
   implementa su protocolo de `channels/contracts.py`, y una función `build_<canal>_adapter` arma
   el conjunto.
3. **Conectarlo** en `integrations/factory.py`, que es el único lugar que conoce la
   configuración y las clases concretas.
4. **Configuración:** las variables del canal en `settings.py` y en `.env.example` (sin
   valores), sus secretos obligatorios en la revisión de `check-config` y su límite de envío en
   `CHANNEL_RATE_LIMITS`.
5. **Pruebas:** unitarias del parser, la firma y el formato; un simulador de la plataforma en
   `tests/e2e/simulators` y la prueba de punta a punta del canal.
6. **Kubernetes:** el disparador de KEDA para la cola del canal, las variables en el
   `ExternalSecret` y, en la NetworkPolicy del dispatcher, la salida hacia la plataforma.
7. **Documentación:** su sección en `docs/integrations` y en el capítulo 3 de este documento.

## Actualizar la documentación

- La documentación vive en el repositorio, en Markdown, junto al código: `README.md`,
  `docs/architecture` (con los ADR), `docs/integrations`, `docs/security`, `docs/deployment`,
  `docs/hardware`, `docs/manual` y `docs/reports`.
- Este documento se genera desde `docs/pdf/capitulos`, con las figuras de `docs/pdf/diagramas.py`:

```bash
pip install -r docs/pdf/requirements.txt
python docs/pdf/generar.py
```

- Cada cambio de comportamiento actualiza el documento que lo describe en el mismo cambio, y se
  anota en `CHANGELOG.md`. Las decisiones de arquitectura nuevas se registran como un ADR.

## Tareas periódicas

| Tarea | Frecuencia | Quién la hace |
|---|---|---|
| Sweeper: republicar lo pendiente y recuperar plazos vencidos | cada 30 segundos | rol `maintenance` |
| Retención, borrados pedidos, cierre de conversaciones viejas, limpieza de cachés y versiones | cada hora | rol `maintenance` |
| Recifrado durante una rotación de claves | en cada vuelta, por lotes | rol `maintenance` |
| Lectura de la versión vigente de la base de conocimiento | cada 30 segundos | todos los roles |
| Ingesta de la base de conocimiento | semanal, o cuando cambian las FAQs | CronJob `ingest` o a mano |
| Respaldo de PostgreSQL | WAL continuo y respaldo completo diario | CloudNativePG |
| Reconstruir las imágenes y escanearlas | en cada versión y cuando salen parches de seguridad | equipo de operación |
| Prueba de restauración del respaldo | trimestral | equipo de operación |
| Revisión de las preguntas sin respuesta y de la base | mensual | responsables del contenido |
