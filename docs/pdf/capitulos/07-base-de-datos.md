# Base de datos

## Motor

| Aspecto | Elección |
|---|---|
| Motor | PostgreSQL 16 |
| Búsqueda vectorial | extensión pgvector, índices HNSW con distancia coseno |
| Acceso desde la aplicación | asyncpg, con consultas parametrizadas y un pool por proceso |
| Esquema | migraciones SQL numeradas (`0001` a `0003`) que aplica el rol `migrate` |
| Producción | CloudNativePG en Kubernetes: un primario y dos réplicas en nodos distintos, con replicación sincrónica |

Una sola base guarda las conversaciones, la auditoría, la base de conocimiento y la caché
semántica. Así hay un solo sistema que respaldar, replicar y vigilar, y las operaciones que tocan
varias tablas (guardar un mensaje y su respuesta, activar una versión de la base) son
transacciones comunes. La decisión está registrada en el ADR 0003.

## Modelo de datos

[[figura:datos|Modelo de datos: tablas, columnas principales y relaciones]]

Las tablas se agrupan en tres partes: la atención (personas, contactos, conversaciones y
mensajes), la operación (auditoría, tokens y rotación de claves) y el conocimiento (versiones de
la base, fragmentos y caché semántica).

## Atención: contactos, conversaciones y mensajes

| Tabla | Qué guarda | Columnas principales |
|---|---|---|
| `persons` | una persona que vinculó contactos de varios canales | `id`, `created_at` |
| `contacts` | una dirección en un canal (un teléfono en WhatsApp, un usuario en Telegram) | `channel`, `account_id`, `address_hash`, `address_enc`, `person_id`, `opted_out`, `erase_requested_at`, `last_seen_at` |
| `conversations` | una atención, de la primera consulta al cierre | `contact_id`, `channel`, `state`, `assigned_to`, `no_info_count`, `handoff_at`, `thread_ref`, `last_activity_at`, `closed_at`, `close_reason` |
| `messages` | cada mensaje que entra o sale, con su estado | `conversation_id`, `contact_id`, `direction`, `author`, `body_enc`, `provider_message_id`, `reply_to_id`, `status`, `attempts`, `lease_until`, `outcome`, `cache_layer`, `kb_version`, `latency_ms` |
| `link_codes` | códigos de un solo uso para vincular canales | `code_hash`, `contact_id`, `expires_at` |

Algunos detalles importantes:

- **La dirección nunca se guarda en claro.** `address_enc` está cifrada para poder responder y
  `address_hash` es el HMAC con que se encuentra al contacto cuando vuelve a escribir.
- **El texto se guarda enmascarado y cifrado** en `body_enc`. Ni la base ni sus respaldos tienen
  el texto legible sin la clave.
- **`seq`** es un número creciente por mensaje que ordena la conversación y sirve al chat web
  para pedir "lo que llegó después del último que vi".
- **`author`** distingue a la persona (`user`), al asistente (`bot`), a un operador (`operator`)
  y a los avisos del sistema (`system`).
- **`outcome`, `cache_layer`, `kb_version` y `latency_ms`** registran cómo se generó cada
  respuesta: si salió de la caché, con qué versión de la base y en cuánto tiempo.

## Operación y conocimiento

| Tabla | Qué guarda | Columnas principales |
|---|---|---|
| `audit_log` | cada acción de la consola y del sistema | `at`, `actor`, `action`, `target_type`, `target_id`, `detail` (nunca datos personales) |
| `oauth_tokens` | tokens de las plataformas que los renuevan (X), cifrados | `provider`, `access_token_enc`, `refresh_token_enc`, `expires_at` |
| `key_rotation` | avance del recifrado de cada tabla | `target`, `key_id`, `last_id`, `rotated`, `failed`, `finished_at` |
| `kb_versions` | cada versión de la base de conocimiento | `version`, `table_name`, `embed_model`, `dimensions`, `chunks`, `source_digest`, `status` |
| `kb_active` | la versión vigente (una sola fila) | `version`, `updated_at` |
| `kb_chunks_<versión>` | los fragmentos de una versión con su embedding | `id`, `title`, `url`, `content`, `question`, `category`, `source`, `embedding` |
| `semantic_cache_d1024` | la caché semántica para embeddings de 1024 dimensiones | `answer_key`, `scope`, `embedding`, `expires_at` |
| `schema_migrations` | las migraciones aplicadas con su checksum | `version`, `checksum` |

Las tablas `kb_chunks_<versión>` las crea la ingesta y la de caché semántica la crea `migrate`
según la dimensión configurada; las demás vienen de las migraciones.

## Relaciones

| Relación | Cardinalidad | Al borrar el padre |
|---|---|---|
| `persons` con `contacts` | una persona, varios contactos (uno por canal vinculado) | el contacto queda sin persona |
| `contacts` con `conversations` | un contacto, varias conversaciones (una abierta a la vez) | se borran sus conversaciones |
| `conversations` con `messages` | una conversación, varios mensajes | se borran sus mensajes |
| `contacts` con `messages` | un contacto, varios mensajes | se borran sus mensajes |
| `messages` con `messages` (`reply_to_id`) | una respuesta apunta al mensaje que contesta | la referencia queda vacía |
| `contacts` con `link_codes` | un contacto, sus códigos de vinculación | se borran los códigos |
| `kb_active` con `kb_versions` | la fila vigente apunta a una versión | no se puede borrar la versión activa |

Las cascadas hacen que borrar un contacto (por pedido de la persona o por vencimiento) elimine en
una sola operación sus conversaciones, mensajes y códigos.

## Índices principales

| Índice | Para qué |
|---|---|
| `contacts_channel_address` (único) | encontrar el contacto por canal y HMAC de la dirección; impide duplicados |
| `conversations_one_open` (único, parcial) | garantiza una sola conversación abierta por contacto, aun con mensajes simultáneos |
| `conversations_state_activity` | las listas de la consola y los cierres por inactividad |
| `messages_inbound_dedup` (único, parcial) | idempotencia: el mismo id de la plataforma no se guarda dos veces |
| `messages_conversation` | leer una conversación en orden |
| `messages_unpublished` (parcial) | el sweeper encuentra lo que quedó sin publicar en la cola |
| `messages_leased` (parcial) | el sweeper encuentra los trabajos cuyo plazo venció |
| `messages_problems` (parcial) | la vista de problemas de la consola (mensajes fallidos o muertos) |
| `messages_outbound_provider` (parcial) | aplicar los avisos de entrega y lectura de las plataformas |
| `kb_chunks_<versión>_hnsw` | búsqueda aproximada por similitud en la base de conocimiento |

Los índices parciales solo incluyen las filas que importan (por ejemplo, los mensajes pendientes),
así que se mantienen chicos aunque la tabla tenga millones de filas.

## Estados

**Mensajes que entran:** `received` (guardado), `processing` (un worker lo tomó con un plazo),
`answered` (tiene respuesta) o `ignored` (no requería respuesta, por ejemplo un duplicado tardío
o una conversación en manos de un operador).

**Mensajes que salen:** `pending` (listo para enviar), `sending` (un dispatcher lo tomó),
`sent` (la plataforma lo aceptó), `delivered` y `read` (avisos de la plataforma), `failed` (falló
y espera un reintento) o `dead` (agotó los reintentos y quedó para revisión).

**Conversaciones:** `bot`, `waiting_human`, `human` y `closed`, con las transiciones del
capítulo 2. El índice `conversations_one_open` asegura que nunca haya dos abiertas para el mismo
contacto.

## Persistencia: qué se guarda en cada lugar

| Componente | Qué guarda | Si se pierde |
|---|---|---|
| PostgreSQL | todo lo que importa: contactos, conversaciones, mensajes, auditoría, base de conocimiento | es lo único que se respalda |
| RabbitMQ | solo identificadores de trabajos pendientes | el sweeper vuelve a publicar lo pendiente desde la base |
| Redis | cachés, locks, límites e idempotencia rápida | se reconstruye solo; la idempotencia la cubre el índice único de PostgreSQL |
| Volumen de modelos | pesos de los modelos | se vuelve a cargar desde el repositorio interno de modelos |

## Conservación y borrado

El rol `maintenance` aplica cada hora los plazos de conservación, configurables:

| Dato | Plazo | Regla |
|---|---|---|
| Mensajes | 90 días | se borran los creados antes del plazo |
| Conversaciones cerradas | 90 días | se borran las cerradas antes del plazo |
| Contactos inactivos | 180 días | se borran con todo lo suyo si no escribieron en ese tiempo |
| Auditoría | 365 días | se borran los registros anteriores |
| Conversaciones con operador sin actividad | 7 días | se cierran |
| Códigos de vinculación | al vencer | se borran los vencidos |
| Caché semántica | 24 horas | se borran las entradas vencidas |
| Versiones de la base | se conservan la activa y la anterior | las demás tablas se eliminan |

Cuando una persona pide la supresión desde el canal (`BORRAR`, con confirmación), el contacto
queda marcado: primero se le envía la confirmación y el mantenimiento lo borra con sus
conversaciones y mensajes en su siguiente vuelta, dentro de la hora. Cuando la ejecuta un
administrador desde la consola, el borrado es inmediato. En los dos casos queda solo el registro
de auditoría, que no tiene datos personales.

## Respaldos y recuperación

Con Compose, un respaldo lógico con `pg_dump`:

```bash
# Respaldo
docker compose exec -T postgres pg_dump -U iamia -Fc iamia > respaldo-$(date +%F).dump

# Recuperación, con los roles de la aplicación detenidos
docker compose stop api worker dispatcher maintenance
docker compose exec -T postgres pg_restore -U iamia -d iamia --clean --if-exists < respaldo.dump
docker compose start api worker dispatcher maintenance
```

En Kubernetes, CloudNativePG archiva el WAL de forma continua y toma un respaldo completo por día
(a las 00:00, hora argentina, desde una réplica) en almacenamiento de objetos compatible con S3,
con 30 días de retención. Eso permite recuperar la base a cualquier momento dentro de ese plazo.

El respaldo tiene los datos personales cifrados: sin `DATA_ENCRYPTION_KEYS` y
`PSEUDONYMIZATION_KEY` no sirve, que es lo que se busca si alguien se lo lleva. Las claves se
guardan aparte, en el gestor de secretos, y se prueban junto con cada restauración. El bucket de
respaldos conviene que esté en otro sitio que el clúster y con versionado o bloqueo de objetos.

## Migraciones

- Cada migración es un archivo SQL numerado. `migrate` las aplica en orden, cada una en su
  transacción, y registra su checksum: si un archivo ya aplicado cambió, se niega a seguir.
- Un lock de PostgreSQL (advisory lock) asegura que una sola réplica migre a la vez.
- `migrate` corre antes que los demás roles: con Compose como servicio previo y en Kubernetes como
  Job previo al despliegue.
- Los cambios se escriben compatibles con la versión anterior de la aplicación: primero se
  agrega (columnas, índices) y, en una versión posterior, se quita lo que ya no se usa. Así, para
  volver atrás alcanza con la imagen anterior.
