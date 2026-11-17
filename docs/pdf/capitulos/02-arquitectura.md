# Arquitectura general

## Vista general

[[figura:arquitectura|Componentes de la plataforma y camino de un mensaje]]

La plataforma está organizada en capas con responsabilidades separadas:

- **Borde.** nginx (en Docker Compose) o ingress-nginx (en Kubernetes) termina TLS, aplica límites
  por IP y deja pasar solo las rutas públicas: webhooks, chat web, API de consulta y consola.
- **Recepción (`api`).** Valida la firma de cada webhook, guarda el mensaje y publica su
  identificador en la cola. Contesta a la plataforma en milisegundos aunque el modelo esté ocupado.
- **Colas (RabbitMQ).** Separan la recepción del procesamiento y del envío: una cola de entrada y
  una de salida por canal.
- **Procesamiento (`worker`).** Decide la respuesta: comandos, derivación a una persona, contexto de
  la conversación y motor de respuesta.
- **Modelos.** vLLM sirve en GPU el modelo de lenguaje y el de embeddings, detrás de una API
  compatible con la de OpenAI.
- **Envío (`dispatcher`).** Entrega cada respuesta por la API de su plataforma, con ritmo,
  reintentos y circuit breaker propios de cada canal.
- **Datos.** PostgreSQL con pgvector guarda conversaciones, base de conocimiento y caché semántica;
  Redis guarda cachés, límites e idempotencia rápida.
- **Mantenimiento (`maintenance`).** Republica lo pendiente, vence derivaciones, aplica la
  retención, recifra los datos después de rotar una clave y limpia las cachés.
- **Consola.** Interfaz web para operadores, con login OIDC (`oauth2-proxy` en Kubernetes) y
  permisos por rol.
- **Observabilidad.** Métricas, logs y trazas de todos los procesos (capítulo 9).

## Monolito modular con roles

Todo el código es un solo paquete de Python (`iamia`) y una sola imagen de contenedor. Cada proceso
arranca con un rol:

| Rol | Responsabilidad | Escala con |
|---|---|---|
| `api` | webhooks de los 8 canales, chat web, `/v1/chat`, API de la consola, salud y métricas | uso de CPU (HPA) |
| `worker` | toma cada mensaje, decide la respuesta y la deja lista para enviar | largo de la cola `iamia.inbound` (KEDA) |
| `dispatcher` | envía las respuestas con ritmo, reintentos y circuit breaker por canal | largo de las colas de salida (KEDA); WhatsApp tiene su propio despliegue |
| `maintenance` | sweeper, derivaciones vencidas, borrados pedidos, retención, rotación de claves | una réplica: sus tareas son idempotentes |
| `migrate`, `ingest` | migraciones del esquema e ingesta de la base de conocimiento | Job y CronJob |

Se eligió un monolito modular en lugar de microservicios porque el dominio es uno solo y el equipo
que lo mantiene es chico. Un paquete con módulos separados por contratos da el mismo aislamiento
lógico sin el costo de versionar, desplegar y observar diez servicios. Los roles sí se despliegan y
escalan por separado, que es lo que importa para la carga. Si un módulo necesitara vida propia
(por ejemplo, el motor de IA), sus contratos permiten separarlo sin reescribir el resto.

## Recorrido de un mensaje

[[figura:secuencia|Recorrido de un mensaje desde que llega hasta que se entrega]]

1. La plataforma envía el webhook a `POST /webhooks/<canal>`.
2. La API valida la firma sobre los bytes recibidos, antes de interpretar el cuerpo.
3. Descarta los duplicados: primero en Redis (`SET NX` con el id del mensaje) y, si Redis no está,
   con el índice único de PostgreSQL.
4. Guarda contacto, conversación y mensaje en una sola transacción, con el texto enmascarado y
   cifrado.
5. Publica el identificador del mensaje en `iamia.inbound` y responde 200.
6. Un worker toma el trabajo y reserva el mensaje con un lease de 180 segundos, para que ningún
   otro lo procese a la vez.
7. Aplica, en orden, la baja, los comandos, la derivación, los límites y el contexto.
8. Si corresponde, consulta el motor de respuesta (capítulo 4): cachés, recuperación en la base de
   conocimiento y generación.
9. Guarda la respuesta y el nuevo estado en una transacción y publica su id en
   `iamia.outbound.<canal>`.
10. El dispatcher del canal espera su turno en el ritmo de la plataforma y envía la respuesta.
11. Marca el mensaje como enviado. Los estados posteriores (entregado, leído, fallido) llegan por
    webhook y solo avanzan, nunca retroceden.

Por las colas viajan solo identificadores: el texto vive cifrado en PostgreSQL.

## Gestor de conversaciones

[[figura:estados|Estados de una conversación]]

Cada contacto tiene como máximo una conversación abierta. El gestor decide qué hacer con cada
mensaje en este orden:

1. **Baja.** Si la persona pidió no recibir mensajes, solo se atiende `ALTA`.
2. **Comandos.** `AYUDA`, `HUMANO`, `PRIVACIDAD`, `BORRAR` (con confirmación), `VINCULAR`, `BAJA` y
   `ALTA`. Tienen que ser el mensaje completo: "¿cómo doy de baja la tarjeta?" es una consulta. Un
   saludo solo recibe la bienvenida sin pasar por el motor.
3. **Derivación.** Si un operador tiene la conversación, el asistente no contesta.
4. **Límites por contacto.** 8 mensajes por minuto y 300 por día, contra el spam y los bucles entre
   bots.
5. **Contenido no textual.** Se avisa que solo se lee texto.
6. **Contexto.** Una pregunta que depende de la anterior ("¿y cuánto sale?") se reescribe como
   pregunta completa con los últimos tres turnos.
7. **Motor de respuesta.** Después de dos consultas seguidas sin información, se ofrece hablar con
   una persona.

El gestor no guarda nada por su cuenta: devuelve una decisión y el worker la persiste en una
transacción. Las conversaciones de distintos canales solo se unen si la persona lo pide, con un
código de un solo uso que genera en un canal y envía desde el otro. El sistema nunca une
identidades por coincidencias de nombre o de teléfono.

## Colas y procesamiento asincrónico

| Elemento | Detalle |
|---|---|
| Intercambio de trabajos | `iamia.jobs` (direct), hacia `iamia.inbound` y una cola `iamia.outbound.<canal>` por canal |
| Tipo de cola | quorum: replicadas en los tres nodos de RabbitMQ |
| Reintentos con espera | intercambio `iamia.retry` con niveles de 1 s, 5 s, 30 s, 2 min, 10 min y 30 min, sin plugins |
| Cola de descarte | `iamia.dead.inbound` e `iamia.dead.outbound`, visibles en la consola |
| Contrapresión | largo máximo con `reject-publish`: si la cola se llena, el mensaje queda en PostgreSQL y el sweeper lo republica |
| Contenido | solo identificadores |

Con una cola por canal, un canal atrasado o una plataforma caída no frenan a los demás.

## Garantías

- **Ningún mensaje guardado queda sin procesar.** Se guarda antes de publicarse y el sweeper
  republica cada 30 segundos lo que no llegó a la cola o quedó con el lease vencido.
- **Nadie recibe dos veces la misma respuesta.** Deduplicación en Redis y en PostgreSQL a la
  entrada, reserva con lease para procesar y progreso por partes para enviar.
- **Un canal no frena a los otros.** Cola, ritmo y circuit breaker propios de cada canal.
- **La API siempre contesta rápido.** Solo valida, guarda y publica. Si PostgreSQL no responde,
  contesta 503 con `Retry-After` y la plataforma reintenta.

## Patrones de resiliencia

| Patrón | Dónde se aplica |
|---|---|
| Reintentos con espera exponencial y jitter | envío por canal y errores transitorios del motor |
| Reintentos con espera en el broker | niveles de 1 s a 30 min |
| Cola de descarte (DLQ) | entrada y salida, con reintento manual desde la consola |
| Circuit breaker | por canal y para el modelo de lenguaje |
| Contrapresión | colas con largo máximo; compuerta de llamadas concurrentes al modelo |
| Idempotencia | Redis `SET NX` más índice único; estados que solo avanzan |
| Single-flight | una sola generación en curso por pregunta igual |
| Timeouts | en cada llamada externa: modelo, embeddings, plataformas y base de datos |
| Aislamiento | colas y despliegues por canal; NetworkPolicy por componente |

## Decisiones de arquitectura

Las decisiones importantes están registradas, con su contexto y sus alternativas, en
`docs/architecture/decisiones/`:

| N.º | Decisión |
|---|---|
| 0001 | Monolito modular con roles en lugar de microservicios |
| 0002 | RabbitMQ con colas quorum para el trabajo asincrónico |
| 0003 | PostgreSQL con pgvector como base de datos y base vectorial |
| 0004 | Guardar primero y publicar después: bandeja de salida, leases y sweeper |
| 0005 | Las identidades de distintos canales solo se vinculan si la persona lo pide |
| 0006 | Modelos en servidores propios, detrás de una API compatible con OpenAI |
| 0007 | Un trabajo que falla sin explicación vuelve con espera y termina en la cola de descarte |
| 0008 | Trazas de OpenTelemetry con el id del mensaje como id de traza |
