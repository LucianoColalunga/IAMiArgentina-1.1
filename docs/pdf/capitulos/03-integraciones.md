# Integraciones

## Un adaptador por canal

[[figura:adaptador|Un canal es un adaptador de cuatro piezas alrededor de un núcleo común]]

Cada canal vive en `backend/iamia/integrations/<canal>/` e implementa los contratos de
`channels/contracts.py`:

| Pieza | Qué hace |
|---|---|
| Verificador (`WebhookVerifier`) | comprueba la firma o el secreto del webhook sobre los bytes recibidos, con comparación en tiempo constante |
| Parser (`WebhookParser`) | traduce el webhook a un `InboundMessage` (canal, id del proveedor, remitente, tipo, texto, hilo, asunto) o a un `StatusUpdate` |
| Formatter (`MessageFormatter`) | adapta la respuesta: largo máximo, partes, formato propio de la plataforma |
| Sender (`MessageSender`) | llama a la API de la plataforma y clasifica sus errores: transitorio, límite de ritmo, permanente o de credenciales |
| `TypingNotifier` (opcional) | muestra "escribiendo..." mientras se genera la respuesta, donde la plataforma lo permite |

Lo demás es común a los ocho canales: idempotencia, colas, reintentos, gestor de conversaciones,
motor de respuesta, derivación y auditoría. El motor y el gestor no importan ningún adaptador, así
que sumar un canal no los modifica.

Un mensaje de WhatsApp, ya traducido al formato común, se ve así (los valores son de ejemplo):

```text
InboundMessage(
    channel=Channel.WHATSAPP,
    provider_message_id="wamid.<id>",         # clave de idempotencia
    sender_id="<BSUID de la persona>",
    account_id="<id del número propio>",      # cuenta que recibió el mensaje
    kind=MessageKind.TEXT,
    sent_at=datetime(2026, 11, 16, 14, 3, 12, tzinfo=UTC),
    text="¿Cómo renuevo el DNI?",
)
```

## Entrada, procesamiento y salida

**Entrada.** La plataforma llama a `POST https://<host>/webhooks/<canal>`. El borde no aplica
límite de tasa a los webhooks (las plataformas reintentan y un límite haría perder mensajes), pero
sí un tamaño máximo de 3 MB. La API valida la firma: si no coincide responde 401, sin detalle, y
suma a la métrica `iamia_webhook_rejected_total`. Si es válida, guarda el mensaje (salvo que sea un
duplicado), lo encola y responde 200.

**Procesamiento.** Un worker toma el mensaje y el gestor de conversaciones decide: comando,
derivación, aviso de contenido no textual o consulta al motor. La respuesta se guarda y se encola
en la cola de salida del canal.

**Salida.** El dispatcher del canal la envía por la API de la plataforma respetando su ritmo,
configurable con `CHANNEL_RATE_LIMITS`. Un error transitorio se reintenta con espera creciente; un
429 respeta el `Retry-After` de la plataforma; un error permanente queda en la consola, en
"Problemas de envío"; un error de credenciales abre el circuito de ese canal sin afectar a los
demás.

**Estados de entrega.** Las plataformas avisan por webhook cuando el mensaje se entregó, se leyó o
falló. Esos estados solo avanzan: uno viejo que llega tarde no pisa a uno nuevo.

## Estado de cada canal

| Canal | Implementado | Probado | Falta para producción |
|---|---|---|---|
| WhatsApp | recepción, envío, estados, plantilla fuera de las 24 h, BSUID, "leído" y "escribiendo" | con simulador, de punta a punta | cuenta de WhatsApp Business verificada, número y plantilla aprobada |
| Messenger | recepción, envío, estados, etiqueta HUMAN_AGENT para operadores | con simulador | app de Meta en modo Live con los permisos aprobados |
| Instagram | recepción, envío en partes de 1000 bytes, etiqueta HUMAN_AGENT | con simulador | cuenta profesional y revisión de la app de Meta |
| Telegram | recepción, envío, comandos y botones, "escribiendo", solo chats privados | con simulador | crear el bot y registrar el webhook |
| X | mensajes directos, CRC, OAuth 2.0 con PKCE y renovación automática, ritmo de 15 cada 15 minutos | con simulador | plan de la API con mensajes directos y aprobación de X para respuestas con IA |
| SMS | recepción, envío en GSM-7 con hasta 3 partes, estados | con simulador | número, registro del remitente y del caso de uso |
| Correo | entrada firmada desde el servidor de correo, salida SMTP en el mismo hilo, filtro de respuestas automáticas | con simulador | servidor de correo con SPF, DKIM y DMARC |
| Chat web | sesiones firmadas, long polling, página propia | completo: no depende de una plataforma externa | nada técnico |

"Con simulador" quiere decir que la prueba de punta a punta habla con un simulador fiel a la API de
cada plataforma (`tests/e2e/simulators/`): recibe los envíos, devuelve estados de entrega y puede
inyectar fallas (errores 500, 429 y credenciales vencidas). **Ningún canal se probó todavía contra
la plataforma real**, porque eso requiere cuentas y aprobaciones de cada empresa. Los datos de
límites y costos de las plataformas son de su documentación a noviembre de 2026 y cambian seguido:
hay que revisarlos antes de habilitar un canal en producción.

## WhatsApp

| Aspecto | Detalle |
|---|---|
| Entrada | webhook de WhatsApp Cloud API en `/webhooks/whatsapp`, campo `messages` |
| Validación | `X-Hub-Signature-256`: HMAC-SHA256 del cuerpo con el app secret; el alta del webhook se verifica con el verify token |
| Idempotencia | `wamid`, el id del mensaje |
| Salida | API de Cloud API con el token de un usuario de sistema; formato de WhatsApp (negrita con asteriscos, sin HTML) |
| Ritmo por defecto | 80 mensajes por segundo |
| Variables | `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` y, opcional, `WHATSAPP_REENGAGEMENT_TEMPLATE` |

- **Ventana de 24 horas.** Se responde libremente dentro de las 24 horas posteriores al último
  mensaje de la persona. Fuera de ese plazo solo se pueden mandar plantillas aprobadas: si hay una
  configurada (categoría utilidad, con una variable en el cuerpo), el sistema la usa para retomar
  el contacto; si no, el envío queda en "Problemas de envío" como `window_closed`.
- **Identidad (BSUID).** Desde 2026 los mensajes traen un identificador por portfolio de negocio y
  el teléfono puede no venir si la persona usa nombre de usuario. El sistema usa el BSUID como
  clave y, si cambia, el aviso de la plataforma reasocia el contacto sin perder la conversación.
- **Experiencia.** Mientras se genera la respuesta, el mensaje se marca como leído y aparece
  "escribiendo...".
- **Costos.** Meta cobra por plantilla entregada, según categoría y país. Las respuestas dentro de
  la ventana de atención, que son casi todas, no tienen costo.

## Messenger

| Aspecto | Detalle |
|---|---|
| Entrada | webhook de la página de Facebook en `/webhooks/messenger`, campos `messages` y `messaging_postbacks` |
| Validación | `X-Hub-Signature-256` con el app secret |
| Idempotencia | `mid` |
| Salida | API de envío de Meta con el token de la página |
| Ritmo por defecto | 40 mensajes por segundo |
| Variables | `MESSENGER_PAGE_ID`, `MESSENGER_PAGE_ACCESS_TOKEN`, `MESSENGER_APP_SECRET`, `MESSENGER_VERIFY_TOKEN` |

- Comparte formato de webhook y API de envío con Instagram, así que los dos usan el mismo
  adaptador base (`integrations/meta/`).
- **Ventana.** Respuesta libre dentro de las 24 horas. Después, solo una persona puede responder,
  hasta 7 días, con la etiqueta HUMAN_AGENT; requiere que Meta apruebe la función y se activa con
  `MESSENGER_HUMAN_AGENT_TAG`. El asistente nunca usa la etiqueta.
- La app tiene que estar en modo Live con acceso avanzado a los permisos de mensajería; en modo
  desarrollo solo responde a cuentas de prueba.

## Instagram

| Aspecto | Detalle |
|---|---|
| Entrada | webhook de Instagram API with Unified Login en `/webhooks/instagram` |
| Validación | `X-Hub-Signature-256` con el app secret |
| Idempotencia | `mid` |
| Salida | API de envío de Meta; hasta 1000 bytes UTF-8 por mensaje (cada tilde ocupa dos) |
| Ritmo por defecto | 40 mensajes por segundo |
| Variables | `INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_VERIFY_TOKEN` |

- Necesita una cuenta profesional con "Permitir acceso a los mensajes" activado.
- Las respuestas largas se dividen en partes que respetan el límite en bytes.
- Misma ventana de 24 horas y misma etiqueta HUMAN_AGENT que Messenger
  (`INSTAGRAM_HUMAN_AGENT_TAG`).

## Telegram

| Aspecto | Detalle |
|---|---|
| Entrada | webhook del bot en `/webhooks/telegram` |
| Validación | `X-Telegram-Bot-Api-Secret-Token`, el secreto que se fija al registrar el webhook |
| Idempotencia | `update_id` |
| Salida | Bot API (`sendMessage`), con indicador de "escribiendo" |
| Ritmo por defecto | 25 mensajes por segundo |
| Variables | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` |

- El bot se crea con @BotFather y el webhook se registra una vez:

```bash
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=https://<host>/webhooks/telegram" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message"]'
```

- Solo se atienden chats privados: en grupos el bot no responde, por alcance y por privacidad.
- Documentos, fotos y audios se registran solo con su tipo; el asistente avisa que lee texto.

## X (mensajes directos)

| Aspecto | Detalle |
|---|---|
| Entrada | X Activity API, suscripción `dm.received`, en `/webhooks/x` |
| Validación | firma HMAC de X y respuesta automática al desafío CRC (`crc_token`) |
| Idempotencia | id del evento |
| Salida | `POST /2/dm_conversations/with/{id}/messages` con un token de usuario OAuth 2.0 (`dm.write`) |
| Ritmo | 15 mensajes cada 15 minutos y 1440 por día para toda la cuenta |
| Variables | `X_ACCOUNT_ID`, `X_WEBHOOK_SECRET`, `X_CLIENT_ID`, `X_CLIENT_SECRET`, `X_OAUTH_REDIRECT_URI`, `X_TOKEN_MODE=refresh` |

- **Autorización inicial.** `docker compose run --rm api x-authorize` imprime la URL de
  autorización (OAuth 2.0 con PKCE). Se abre con la cuenta de X de la organización y el token de
  renovación queda guardado, cifrado, en la base. Después se renueva solo; un lock evita que dos
  procesos lo renueven a la vez.
- **Capacidad.** Es el canal de menor capacidad: el excedente espera en `iamia.outbound.x` y sale
  cuando hay cupo. Conviene avisar en el perfil que la respuesta puede demorar.
- **Política de X.** El bot solo responde a un mensaje previo, ofrece la baja y las respuestas
  generadas con IA necesitan aprobación previa de X. El acceso a la API de mensajes directos es
  pago.
- Durante la transición se acepta también el formato de la Account Activity API, que X da de baja
  el 28 de febrero de 2027.

## SMS

| Aspecto | Detalle |
|---|---|
| Entrada | webhook de Twilio (o un proveedor con la misma API) en `/webhooks/sms`, que también recibe los estados |
| Validación | `X-Twilio-Signature`: HMAC-SHA1 de la URL pública y los parámetros |
| Idempotencia | `MessageSid` |
| Salida | API de mensajes del proveedor; texto en GSM-7, hasta 3 segmentos (`SMS_MAX_SEGMENTS`) |
| Ritmo por defecto | 10 mensajes por segundo |
| Variables | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` (o `TWILIO_MESSAGING_SERVICE_SID`), `SMS_WEBHOOK_URL` |

- `SMS_WEBHOOK_URL` tiene que ser exactamente la URL registrada en el proveedor: la firma se
  calcula sobre la URL pública y, detrás de un proxy, la que ve el servidor no coincide.
- Las tildes agudas pasan un SMS a UCS-2, con la mitad de caracteres por segmento; por eso las
  respuestas se transliteran a GSM-7 y se recortan a tres segmentos.
- Antes de producción hay que consultar con el proveedor los requisitos para enviar a números
  argentinos (registro del remitente y del caso de uso).

## Correo electrónico

| Aspecto | Detalle |
|---|---|
| Entrada | el servidor de correo entrega cada mensaje a `scripts/email-a-iamia.py`, que lo publica firmado en `/webhooks/email` |
| Validación | HMAC-SHA256 con timestamp, válido 5 minutos |
| Idempotencia | `Message-ID` |
| Salida | SMTP autenticado con STARTTLS, en el mismo hilo (`In-Reply-To` y `References`) y con el asunto del último correo |
| Ritmo por defecto | 10 mensajes por segundo |
| Variables | `EMAIL_INBOUND_SECRET`, `EMAIL_FROM_ADDRESS`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` |

- No hace falta exponer IMAP ni guardar contraseñas de buzones. El script usa solo la biblioteca
  estándar de Python, devuelve a Postfix los códigos que entiende (75 = reintentar más tarde) y,
  si el correo supera 3 MB, manda los adjuntos vacíos.
- **Bucles.** Se ignoran respuestas automáticas (`Auto-Submitted`, `Precedence: bulk`) y rebotes,
  y las respuestas propias salen marcadas como automáticas, para que dos sistemas no se contesten
  entre sí indefinidamente.
- Antes de producción: SPF, DKIM y DMARC del dominio remitente; sin eso, las respuestas terminan
  en spam.

## Chat web

| Endpoint | Para qué |
|---|---|
| `POST /webchat/sessions` | crea una sesión y devuelve un token firmado con HMAC que vence a las 24 horas; hasta 30 sesiones nuevas por IP por hora |
| `POST /webchat/messages` | envía un mensaje, que sigue el mismo camino que los demás canales |
| `GET /webchat/messages?after=<n>` | long polling de hasta 25 segundos; cuando el worker guarda la respuesta, avisa por Redis a la réplica de la API que tiene al navegador esperando |

La página `/chat/` la sirve la misma plataforma. No usa scripts de terceros ni código en línea y
tiene una Content Security Policy estricta (`script-src 'self'`).

## API de consulta `/v1/chat`

Una consulta de un solo turno, sin conversación, compatible con la de la versión 1 y pensada para
integrar el asistente en otra aplicación. Usa el mismo motor y las mismas cachés. Exige la cabecera
`X-Edge-Auth`, que agrega el borde, y tiene un límite de 2 pedidos por segundo por IP.

## Habilitar un canal, paso a paso

1. Conseguir la cuenta y las credenciales en la plataforma (detalle por canal arriba).
2. Completar sus variables en `.env` (o en el gestor de secretos) y agregar el canal a
   `CHANNELS_ENABLED`, por ejemplo `CHANNELS_ENABLED=webchat,telegram,whatsapp`.
3. Revisar la configuración: `docker compose run --rm --no-deps api check-config` avisa si falta un
   secreto o si quedó un valor de ejemplo.
4. Aplicar la configuración con `docker compose up -d`, que recrea los roles cuya configuración
   cambió (en Kubernetes, un nuevo despliegue de la app).
5. Registrar el webhook en la plataforma con la URL pública `https://<host>/webhooks/<canal>`.
6. Mandar un mensaje de prueba y seguirlo en la consola y en los logs (capítulo 9).
