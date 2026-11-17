# Seguridad

Este capítulo explica qué se protege, con qué controles y qué queda pendiente. Está escrito para
quien tenga que revisar o aprobar el sistema, no como guía de ataque: los detalles de las reglas
de detección, los secretos y las direcciones internas no forman parte de esta documentación.

> No es asesoramiento legal. Las referencias a la Ley 25.326 muestran cómo el diseño acompaña sus
> principios; el encuadre final (inscripción de la base, finalidad, textos de información a la
> persona) lo tiene que validar el área legal que corresponda.

## Principios

| Principio | Cómo se aplica |
|---|---|
| Zero Trust | ningún componente confía en otro por estar en la misma red: los webhooks llevan firma, la consola usa OIDC, los servicios internos tienen credenciales propias y en Kubernetes la red está cerrada por defecto |
| Mínimo privilegio | contenedores sin root, sin capabilities y con sistema de archivos de solo lectura; roles de consola separados (quien audita no lee conversaciones) |
| Minimización | se guarda lo necesario para responder y atender: el texto enmascarado y la dirección cifrada, sin adjuntos |
| Defensa en profundidad | ningún control es el único: si falla la detección de manipulación, sigue el umbral de relevancia; si falla ese, el control de salida y el anclaje en la base |
| Fallar cerrado | una firma inválida, un token vencido o una configuración insegura en producción cortan el pedido o impiden arrancar; nunca se deja pasar con un aviso |

## Activos y amenazas

| Activo | Por qué importa |
|---|---|
| Mensajes de las personas | pueden contener datos personales aunque se pida no mandarlos |
| Direcciones (teléfono, usuario, correo) | identifican a la persona |
| Credenciales de los canales | con ellas se puede escribirle a la gente en nombre de quien opera el servicio |
| Claves de cifrado y de seudonimización | protegen todo lo anterior |
| Base de conocimiento | si alguien la altera, el asistente difunde información falsa |
| Disponibilidad | el canal de consulta tiene que funcionar en los picos |

El modelo de amenazas sigue las categorías STRIDE (suplantación, manipulación, repudio,
divulgación, denegación de servicio y elevación de privilegios):

| Superficie | Amenaza | Mitigación |
|---|---|---|
| Webhooks | pedidos falsos para inyectar mensajes | firma HMAC de cada plataforma sobre los bytes recibidos, comparación en tiempo constante, 401 sin detalle; métrica y alerta de rechazos |
| Webhooks | reenvío de un pedido capturado | idempotencia por id del canal (Redis más una restricción única en PostgreSQL); el correo además lleva fecha con tolerancia de 5 minutos |
| Webhooks | saturar la API | la API solo valida y encola; colas con contrapresión; cuerpo de hasta 3 MB; límites por contacto (8 por minuto, 300 por día) |
| Chat web y `/v1/chat` | abuso automatizado | límites por IP en el borde, sesiones por IP acotadas, cupo diario de tokens por cliente, `X-Edge-Auth` en `/v1/chat` |
| Motor de IA | manipular al asistente (prompt injection) | detección antes del modelo, umbral de relevancia, prompt con delimitadores, control de salida y anclaje (capítulo 4) |
| Motor de IA | respuestas inventadas o enlaces falsos | respuesta solo con lo recuperado, verificación de anclaje, enlaces permitidos solo a dominios oficiales |
| Base de conocimiento | contenido alterado | ingesta solo desde FAQs propias o páginas oficiales listadas; versiones con reemplazo atómico |
| Consola | acceso indebido a conversaciones | OIDC verificado en la API, RBAC por permiso, direcciones enmascaradas, auditoría de cada lectura y acción |
| Consola | negar una acción realizada | auditoría con sujeto, acción, objeto y hora; el auditor la consulta sin poder modificarla |
| Datos en reposo | robo de un respaldo o de un disco | texto y direcciones cifrados con AES-256-GCM; búsqueda de direcciones por HMAC con otra clave |
| Logs y métricas | filtración por la observabilidad | los logs nunca llevan texto ni direcciones; etiquetas de métricas acotadas |
| Infraestructura | movimiento lateral desde un contenedor | NetworkPolicy por componente, sin root, sin capabilities, solo lectura, `no-new-privileges` |
| Cadena de suministro | dependencia o imagen comprometida | versiones fijadas, imágenes por versión o digest, `pip-audit`, Trivy y gitleaks antes de cada versión |

## Autenticación

Cada punto de entrada se autentica de una forma distinta, según quién llega por él:

| Entrada | Quién llega | Cómo se autentica |
|---|---|---|
| `/webhooks/<canal>` | las plataformas | firma HMAC del cuerpo con el secreto de la app (Meta, X), token secreto en cabecera (Telegram), firma de Twilio (SMS), firma HMAC con fecha que agrega el script de entrada del servidor de correo |
| `/admin/api` (consola) | operadores, supervisores, auditores y administradores | token OIDC (JWT) del proveedor de identidad, verificado en la API |
| `/webchat` | personas desde el navegador | token de sesión aleatorio firmado con HMAC-SHA256, vence a las 24 horas |
| `/v1/chat` | integraciones simples y pruebas | cabecera `X-Edge-Auth` que agrega el borde, más límites por IP |
| Servicios internos | los roles de la aplicación | usuario y contraseña propios en PostgreSQL, RabbitMQ y Redis; clave de API para los modelos locales |

**Consola con OIDC.** En Kubernetes la consola queda detrás de `oauth2-proxy`, pero la API no
confía en el proxy: verifica el JWT en cada pedido contra las claves públicas del proveedor (JWKS)
y exige firma RS256 o ES256, emisor, audiencia, vencimiento, fecha de emisión y sujeto. Un token
con `alg: none` se rechaza. Los roles salen de un claim configurable (por defecto
`realm_access.roles`, el formato de Keycloak) y se ignoran los que no se conocen. El token de
desarrollo (`python -m iamia dev-token`) solo funciona con `APP_ENV=development`.

**Sesiones.** La sesión de la consola dura 8 horas y el token del proveedor se renueva cada pocos
minutos; la cookie de oauth2-proxy es `__Host-`, `Secure` y `SameSite=Lax`. La sesión del chat
web es un identificador aleatorio de 128 bits, firmado y con vencimiento.

## Autorización: roles y permisos

Cada acción de la consola pide un permiso, no un rol. Así se puede reorganizar qué hace cada rol
sin tocar el código de las acciones. Los roles vienen del proveedor de identidad.

| Permiso | Qué habilita | Operador | Supervisor | Auditor | Admin |
|---|---|---|---|---|---|
| `conversations:read` | ver la lista y el detalle de las conversaciones | sí | sí | no | sí |
| `conversations:claim` | tomar una conversación | sí | sí | no | sí |
| `conversations:reply` | responder en una conversación tomada | sí | sí | no | sí |
| `conversations:close` | devolverla al asistente o cerrarla | sí | sí | no | sí |
| `conversations:reassign` | tomar, devolver o cerrar conversaciones de otra persona | no | sí | no | sí |
| `channels:read` | ver canales, colas y el resumen de 24 horas | no | sí | sí | sí |
| `audit:read` | consultar la auditoría | no | sí | sí | sí |
| `dead_letters:manage` | ver y reintentar los mensajes con problemas | no | no | no | sí |
| `contacts:delete` | borrar un contacto con todos sus datos | no | no | no | sí |

Un operador solo responde en las conversaciones que tomó; para responder hay que tomarla primero.
La primera lectura de cada conversación por cada persona queda auditada (las relecturas dentro de
los 10 minutos siguientes no repiten el registro).

## Cifrado y seudonimización

| Dato | Protección |
|---|---|
| Texto de los mensajes | enmascarado de datos personales y luego AES-256-GCM |
| Direcciones (teléfono, usuario, correo) | AES-256-GCM para leerlas y HMAC-SHA256 con otra clave para buscarlas |
| Tokens de OAuth guardados (X) | AES-256-GCM |
| Tráfico desde internet | TLS en el borde (nginx o ingress) |
| PostgreSQL en Kubernetes | TLS, servido por CloudNativePG |
| Respaldos | el texto y las direcciones ya van cifrados por la aplicación; las claves se guardan aparte, nunca junto al respaldo |

Cada valor cifrado lleva el identificador de la clave con que se cifró, y el cifrado se ata a su
contexto con datos asociados (AAD): un valor cifrado para un mensaje no se puede copiar a otro
registro y descifrarse ahí. Las claves se configuran como `id:clave` en `DATA_ENCRYPTION_KEYS`; la
primera cifra y todas descifran.

La seudonimización usa una clave distinta (`PSEUDONYMIZATION_KEY`): la base guarda el HMAC de cada
dirección para encontrar al contacto cuando vuelve a escribir, pero sin esa clave no permite
buscar a alguien por su teléfono. Los trabajos que pasan por RabbitMQ llevan solo
identificadores: el contenido nunca pasa por el broker ni queda en sus discos.

## Rotación de claves y credenciales

1. Generar una clave nueva con `gen-secrets`.
2. Agregarla al principio de `DATA_ENCRYPTION_KEYS`, con las anteriores detrás, y desplegar. Lo
   nuevo se cifra con ella y lo viejo se sigue leyendo.
3. El rol `maintenance` recifra en segundo plano, por lotes, las direcciones, los mensajes y los
   tokens guardados con claves anteriores. El avance se ve en el log (`key_rotation_done`) y en la
   métrica `iamia_key_rotation_rows_total`.
4. La vuelta que confirma el final espera una hora desde el cambio (`KEY_ROTATION_GRACE_MINUTES`),
   para alcanzar lo que escribieron los procesos que todavía tenían la configuración anterior.
5. Retirar la clave vieja recién cuando `python -m iamia key-usage` muestra que ya nada la usa.

En Kubernetes, con despliegue gradual, la clave se agrega en dos pasos: primero al final de la
lista (todos los pods la conocen y siguen cifrando con la anterior) y, terminado ese despliegue,
al principio. Así ningún pod viejo encuentra un dato cifrado con una clave que no conoce. El
procedimiento se probó en un simulacro con datos reales de prueba (capítulo 10).

Las credenciales de los canales se cambian en la plataforma y en el gestor de secretos; después se
reinicia la API (si es un secreto de firma) o el dispatcher (si es de envío). La clave de
seudonimización solo se cambia ante un compromiso y con un plan de recálculo, porque cambiarla
rompe la búsqueda de los contactos existentes.

## Secretos y separación de entornos

- **Nunca en Git.** `.gitignore` excluye los `.env` salvo los `.example`, que no tienen valores.
  El repositorio se revisa con gitleaks antes de cada versión.
- **Instalación local.** `.env` con claves generadas por `gen-secrets`, legible solo por el
  usuario que corre Docker.
- **Kubernetes.** Los secretos viven en Vault y External Secrets Operator los copia como `Secret`
  del namespace. Los manifiestos solo tienen referencias.
- **Verificación al arrancar.** `python -m iamia check-config` (y el arranque de cada rol) revisa
  que las claves de cifrado estén definidas, que las contraseñas no sean las de ejemplo, que OIDC
  esté configurado si la consola está habilitada y que cada canal habilitado tenga sus secretos.
  En producción cualquier problema impide arrancar.
- **Entornos separados.** `APP_ENV` distingue desarrollo, staging y producción. Cada entorno de
  Kubernetes tiene su overlay, su clúster o namespace y sus secretos: ninguno comparte claves,
  credenciales ni base de datos con otro. Las pruebas generan claves nuevas en cada máquina y usan
  simuladores, nunca cuentas reales.

## Datos personales (Ley 25.326)

| Principio | Cómo lo acompaña el diseño |
|---|---|
| Calidad y minimización (art. 4) | se guarda el texto enmascarado, sin adjuntos; las respuestas salen solo de la base de conocimiento |
| Información a la persona (art. 6) | el comando `PRIVACIDAD` explica qué se guarda, para qué y por cuánto tiempo; el texto final lo define el área legal |
| Seguridad (art. 9) | cifrado en reposo, seudonimización, control de acceso por roles, auditoría y red cerrada, en línea con las medidas recomendadas por la AAIP |
| Confidencialidad (art. 10) | los operadores ven direcciones enmascaradas; logs y métricas sin datos personales; el auditor no lee conversaciones |
| Cesión y transferencia internacional (arts. 11 y 12) | los modelos corren en servidores propios: las consultas no se mandan a servicios de IA de terceros. Los mensajes sí pasan por la plataforma que la persona eligió para escribir |
| Acceso, rectificación y supresión (arts. 14 a 16) | la persona pide la supresión desde el canal (`BORRAR`, con confirmación) y un administrador puede hacerlo desde la consola; se borran contacto, conversaciones y mensajes y queda solo el registro de auditoría |
| Plazos de conservación | mensajes 90 días, contactos inactivos 180 días, auditoría 365 días (configurables); el mantenimiento los aplica cada hora |
| Registro de bases (art. 21) | la base de conversaciones debe inscribirse en el Registro Nacional de Bases de Datos antes de producción |

El enmascarado reemplaza DNI, CUIL, teléfonos, correos, tarjetas, CBU y pasaportes por marcadores
(`[DNI]`, `[EMAIL]`, ...) antes de que el texto llegue al modelo y antes de guardarlo. El sistema
nunca une conversaciones de distintos canales por su cuenta: solo lo hace si la persona lo pide
con un código de un solo uso generado en un canal y enviado desde el otro.

## Auditoría

Toda acción de la consola queda en la tabla `audit_log`: quién (el sujeto del token), qué acción,
sobre qué objeto, cuándo y un detalle sin datos personales. Se registran la primera lectura de cada
conversación, tomar, responder, devolver, cerrar, reintentar un mensaje y borrar un contacto. El
registro no guarda texto de mensajes ni direcciones, se conserva 365 días y el rol auditor lo
consulta sin poder modificarlo ni leer las conversaciones.

## Protección frente a ataques web

| Ataque | Cómo se evita |
|---|---|
| Inyección SQL | todas las consultas pasan los valores como parámetros (`$1`, `$2`, ...). Solo se arman con texto expresiones fijas del código y nombres de tablas internas validados, nunca datos que llegan de afuera |
| XSS | el chat web y la consola arman la página con `textContent` y elementos del DOM, nunca con `innerHTML`; CSP `script-src 'self'` sin `unsafe-inline`; la API responde solo JSON con `nosniff` |
| CSRF | en producción la sesión de la consola es una cookie `SameSite=Lax`, que el navegador no envía en los POST y DELETE iniciados desde otro sitio, y todas las acciones usan esos métodos. En desarrollo el token viaja en la cabecera `Authorization` |
| SSRF | ningún componente descarga una URL que mande una persona y los adjuntos no se descargan. La ingesta solo pide páginas `https` de dominios oficiales y valida cada redirección; en Kubernetes la salida excluye las redes privadas |
| Prompt injection | defensa en capas descrita en el capítulo 4; las reglas de detección no se publican |
| Exposición de información | errores sin detalle interno, 503 con `Retry-After` en lugar de un 500 con traza, logs sin texto ni direcciones, `/metrics` cerrado al exterior |
| Abuso y DDoS | límites por IP en el borde (`/v1/chat` 2 pedidos por segundo, chat web 10), por contacto y por cliente; la API solo encola, así que un pico no la tumba. Un ataque volumétrico se frena antes de llegar, con la protección del proveedor de red o una CDN |

La API además normaliza Unicode, elimina caracteres de control e invisibles, limita el largo de
cada mensaje (1000 caracteres, 256 tokens) y agrega cabeceras de seguridad en todas las
respuestas (`nosniff`, CSP, `frame-ancestors 'none'`, HSTS en producción).

## Seguridad de la infraestructura

- **Red.** En Kubernetes cada namespace arranca con `NetworkPolicy` que niega todo, y cada
  componente recibe solo las conexiones que necesita. Solo el ingress publica puertos; `/metrics`
  y los paneles de RabbitMQ y Grafana no se exponen.
- **Contenedores.** Usuario sin privilegios, sistema de archivos de solo lectura, sin
  capabilities y con `no-new-privileges`. Las imágenes base están fijadas por versión o digest.
- **Análisis antes de cada versión.** gitleaks sobre el repositorio y su historial, Trivy sobre
  archivos, imágenes y manifiestos, y `pip-audit` sobre las dependencias. Los resultados de esta
  versión están en el capítulo 10.

## Gestión de incidentes

1. **Detectar.** Las alertas (cada una con su runbook) avisan errores, colas que crecen, webhooks
   rechazados, fallas de entrega y datos que no se pueden descifrar.
2. **Contener.** Según el caso: deshabilitar un canal (`CHANNELS_ENABLED`) o la consola, revocar
   el acceso de una persona en el proveedor de identidad, rotar credenciales o claves, o bajar a
   cero las réplicas de un rol.
3. **Investigar.** Logs (30 días), trazas (7 días) y auditoría (365 días). Ninguno tiene texto de
   mensajes ni direcciones, así que se pueden compartir con quien investiga.
4. **Comunicar.** Si hay datos personales comprometidos, avisar al responsable de la base y al
   área legal, que define las comunicaciones a las personas afectadas y a la AAIP.
5. **Cerrar.** Registrar causa, impacto y corrección, y sumar una prueba o una alerta para que no
   se repita.

## Riesgos residuales

| Riesgo | Situación | Mejora prevista |
|---|---|---|
| Detección de manipulación por reglas | frena los intentos conocidos y las otras capas limitan el daño, pero no es infalible | clasificador local dedicado y pruebas de red teaming periódicas |
| Tráfico interno sin mTLS | autenticado por servicio y restringido por NetworkPolicy, pero sin cifrar (salvo PostgreSQL) | service mesh con identidad de servicio |
| Redis sin réplica | es solo caché e idempotencia rápida; si se cae, la idempotencia la cubre PostgreSQL | réplica con Sentinel si la caché se vuelve crítica |
| Plataformas de terceros | los mensajes pasan por las empresas de cada canal | informarlo a la persona (comando `PRIVACIDAD`) |
| Calidad de las respuestas | una FAQ desactualizada produce una respuesta desactualizada | revisión periódica de la base y de las preguntas sin respuesta |
