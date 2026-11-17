# Seguridad y datos personales

Este documento describe qué se protege, de quién, con qué controles y qué queda pendiente. Está
escrito para quien tenga que revisar o aprobar el sistema, no como guía de ataque: los detalles de
las reglas de detección y de los secretos no están acá.

> No es asesoramiento legal. Las referencias a la Ley 25.326 explican cómo el diseño acompaña sus
> principios; el encuadre final (registro de la base, finalidad, textos de información a la
> persona) lo tiene que validar el área legal de la organización.

## Principios

- **Zero Trust.** Ningún componente confía en otro por estar en la misma red. Cada llamada se
  autentica (webhooks con firma, consola con OIDC, servicios internos con credenciales propias) y
  en Kubernetes la red está cerrada por defecto (`NetworkPolicy` default deny) con permisos
  explícitos por componente.
- **Mínimo privilegio.** Contenedores sin root, sin capabilities y con sistema de archivos de solo
  lectura. Roles de consola separados: quien audita no lee conversaciones.
- **Minimización.** Se guarda lo necesario para responder y atender: el texto enmascarado, la
  dirección cifrada, nada de adjuntos.
- **Defensa en profundidad.** Ningún control es el único: si falla la detección de manipulación,
  sigue el umbral de relevancia; si falla ese, sigue el control de salida y el anclaje en la base
  de conocimiento.
- **Fallar cerrado.** Una firma inválida, un token vencido o una configuración insegura en
  producción cortan el pedido o impiden arrancar; no se "deja pasar con un aviso".

## Activos

| Activo | Por qué importa |
|---|---|
| Mensajes de las personas | pueden contener datos personales aunque se pida no mandarlos |
| Direcciones (teléfono, usuario, correo) | identifican a la persona |
| Credenciales de canales (tokens de Meta, X, Telegram, Twilio, SMTP) | con ellas se puede escribirle a la gente en nombre de la organización |
| Claves de cifrado y de seudonimización | protegen todo lo anterior |
| Base de conocimiento | si alguien la altera, el asistente difunde información falsa |
| Disponibilidad | el canal de consulta tiene que funcionar en los picos |

## Modelo de amenazas

Superficies y amenazas principales (categorías STRIDE entre paréntesis), con su mitigación:

| Superficie | Amenaza | Mitigación |
|---|---|---|
| Webhooks | webhooks falsos para inyectar mensajes (suplantación) | firma HMAC de cada plataforma sobre los bytes recibidos, comparación en tiempo constante, 401 sin detalle; métrica y alerta de rechazos |
| Webhooks | reenvío de un pedido capturado (repetición) | idempotencia por id del canal (Redis + restricción única en PostgreSQL); el correo además lleva timestamp con tolerancia de 5 minutos |
| Webhooks | saturar la API (denegación de servicio) | la API solo valida y encola; colas con contrapresión; límite de cuerpo de 3 MB; límites por contacto (8 por minuto, 300 por día) |
| Chat web y `/v1/chat` | abuso automatizado | límites por IP en nginx, límite de sesiones por IP, cupo diario de tokens por cliente, `X-Edge-Auth` en `/v1/chat` |
| Motor de IA | manipular al asistente para que ignore sus reglas (prompt injection) | detección por reglas antes de llamar al modelo, consultas fuera de tema rechazadas por relevancia, prompt con delimitadores que el usuario no puede cerrar, control de salida (fuga del prompt, código canario), anclaje de la respuesta en el contexto |
| Motor de IA | respuestas inventadas o enlaces a sitios falsos | respuesta solo con lo recuperado de la base, umbral de relevancia, verificación de anclaje, enlaces permitidos solo a `gob.ar` |
| Base de conocimiento | contenido alterado (manipulación) | ingesta solo desde FAQs propias o páginas oficiales listadas; índice versionado y reemplazo atómico |
| Consola | acceso indebido a conversaciones (divulgación) | OIDC con firma RS256/ES256 verificada en la API (no se confía en el proxy), issuer y audiencia obligatorios, `alg: none` rechazado, RBAC por permiso, direcciones enmascaradas, auditoría de cada lectura y acción |
| Consola | operador que niega lo que hizo (repudio) | auditoría con sujeto del token, acción, objeto y hora; el auditor la consulta sin poder modificarla |
| Datos en reposo | robo de un backup o del disco | texto y direcciones cifrados con AES-256-GCM (claves con id, rotables); búsqueda de direcciones por HMAC con clave aparte |
| Logs y métricas | filtración de datos personales por observabilidad | los logs nunca llevan texto ni direcciones; labels de métricas acotados (canal, resultado) |
| Infraestructura | movimiento lateral desde un contenedor comprometido | NetworkPolicy por componente, sin root, sin capabilities, solo lectura, `no-new-privileges`; solo nginx/ingress publica puertos |
| Cadena de suministro | dependencia o imagen comprometida | versiones fijadas, imágenes por versión o digest, `pip-audit`, Trivy y gitleaks antes de cada versión |
| Secretos | credenciales en el repositorio | `.env*` ignorados por Git (salvo los `.example` sin valores), gestor de secretos en producción, escaneo con gitleaks |

## Controles por capa

**Borde (nginx o ingress).** TLS, límites por IP (`/v1/chat` 2 r/s, chat web 10 r/s), cuerpo
máximo de 3 MB, `/metrics` cerrado al exterior, cabecera `X-Edge-Auth` con un secreto que la API
exige en `/v1/chat`. Los logs del borde no guardan la query string (la verificación de Meta trae
el verify token en la URL).

**API.** Valida firmas antes de interpretar el cuerpo; normaliza Unicode, elimina caracteres de
control e invisibles y limita largo (1000 caracteres, 256 tokens). Cabeceras de seguridad en
todas las respuestas (`nosniff`, CSP `default-src 'none'` en la API y `script-src 'self'` sin
`unsafe-inline` en las páginas, `frame-ancestors 'none'`, HSTS en producción). Errores sin
detalles internos. Si PostgreSQL o Redis no responden, contesta 503 con `Retry-After` en vez de
un 500 con traza.

**Datos.** Antes de guardar, el texto pasa por el enmascarado de datos personales (DNI, CUIL,
teléfono, correo, tarjeta, CBU, pasaporte) y se cifra con AES-256-GCM. Las claves se configuran
como `id:clave`; para rotar se agrega la nueva primero y las anteriores siguen descifrando. Las
direcciones se cifran y se indexan con HMAC-SHA256 con una clave distinta
(`PSEUDONYMIZATION_KEY`), así que la base no permite buscar a alguien por su teléfono sin esa
clave.

**Colas.** Los trabajos en RabbitMQ llevan solo identificadores: el contenido nunca pasa por el
broker ni queda en sus discos.

**Consola.** Detrás de `oauth2-proxy` en Kubernetes, pero la API valida el JWT igual (firma,
emisor, audiencia, vencimiento) contra el JWKS del proveedor. El token del modo de desarrollo
solo funciona con `APP_ENV=development`.

**Arranque.** `python -m iamia check-config` (y el arranque de cada rol) revisa: claves de
cifrado definidas, contraseñas que no sean las de ejemplo (las del código y las del Compose
local), OIDC configurado si la consola está habilitada, secretos de cada canal habilitado. En
producción cualquier problema impide arrancar.

## Ataques web comunes

| Ataque | Cómo se evita |
|---|---|
| Inyección SQL | todas las consultas pasan los valores como parámetros de asyncpg (`$1`, `$2`, ...). Solo se arman con texto expresiones fijas del código y nombres de tablas internas (versiones de la base de conocimiento, validadas con una expresión regular), nunca datos que llegan de afuera |
| XSS | el chat web y la consola arman la página con `textContent` y elementos del DOM, nunca con `innerHTML`; CSP `script-src 'self'` sin `unsafe-inline`; la API responde solo JSON, con `nosniff` |
| CSRF | en producción la sesión de la consola es la cookie de oauth2-proxy (`__Host-`, `Secure`, `SameSite=Lax`), que el navegador no envía en los POST y DELETE iniciados desde otro sitio; todas las acciones de la consola usan esos métodos. En desarrollo el token viaja en la cabecera `Authorization`, que otro sitio no puede agregar |
| SSRF | ningún componente descarga una URL que mande una persona y los adjuntos no se descargan. La ingesta solo pide páginas `https` de dominios oficiales y valida cada redirección antes de seguirla; en Kubernetes, la salida a internet excluye las redes privadas, link-local y de direcciones compartidas |
| Sesiones | consola: sesión de 8 horas con el token del proveedor renovado cada pocos minutos y JWT verificado en cada pedido. Chat web: id de sesión aleatorio de 128 bits, firmado con HMAC-SHA256 y con vencimiento a las 24 horas |
| OAuth 2.0 y rotación de tokens | la consola usa OIDC (sobre OAuth 2.0). X usa OAuth 2.0 con tokens de renovación que cambian en cada uso: se guardan cifrados y un lock evita que dos procesos los renueven a la vez |
| Exposición de información | errores sin detalle interno, logs sin texto ni direcciones, `/metrics` cerrado al exterior, direcciones enmascaradas en la consola |
| Abuso y DDoS | límites por IP en el borde, por contacto y por cliente; la API solo encola, así que un pico no la tumba. Un ataque volumétrico se frena antes de llegar: protección del proveedor de red o de una CDN delante del ingress |

## Datos personales (Ley 25.326)

| Principio | Cómo lo acompaña el diseño |
|---|---|
| Calidad y minimización (art. 4) | se guarda el texto enmascarado, sin adjuntos; las respuestas salen solo de la base de conocimiento oficial |
| Información a la persona (art. 6) | el comando `PRIVACIDAD` explica qué se guarda, para qué y por cuánto tiempo; el texto final lo define el área legal |
| Seguridad (art. 9) | cifrado en reposo, seudonimización, control de acceso por roles, auditoría, red cerrada; alineado con las medidas recomendadas por la AAIP (Resolución 47/2018) |
| Confidencialidad (art. 10) | operadores ven direcciones enmascaradas; logs y métricas sin datos personales; el auditor no lee conversaciones |
| Cesión y transferencia internacional (arts. 11 y 12) | los modelos corren en servidores propios: las consultas no se mandan a APIs de IA de terceros. Los mensajes sí pasan por la plataforma que la persona eligió para escribir (Meta, X, Telegram, el proveedor de SMS), que tiene sus propias condiciones |
| Acceso, rectificación y supresión (arts. 14 a 16) | la persona puede pedir la supresión desde el canal (`BORRAR`, con confirmación) y un administrador puede hacerlo desde la consola; el borrado elimina contacto, conversaciones y mensajes y deja solo el registro de auditoría |
| Plazos de conservación | mensajes 90 días, contactos inactivos 180 días, auditoría 365 días (configurables); la tarea de mantenimiento los aplica cada hora |
| Registro de bases (art. 21) | la base de conversaciones debe inscribirse en el Registro Nacional de Bases de Datos antes de producción |

**Vinculación de identidades.** El sistema nunca une conversaciones de distintos canales por su
cuenta (por nombre, teléfono o cualquier coincidencia). Solo lo hace si la persona lo pide con un
código de un solo uso generado en un canal y enviado desde el otro.

## Secretos

- Nunca en Git. `.gitignore` excluye `.env*` salvo los `.example`, que no tienen valores. El
  repositorio se revisa con gitleaks (resultados en `docs/reports/auditoria-final.md`).
- **Local:** `.env` con claves generadas por `docker compose run --rm --no-deps api gen-secrets`; dejarlo
  legible solo para el usuario que corre Docker (`chmod 600 .env`).
- **Kubernetes:** Vault → External Secrets Operator → `Secret` del namespace. Los manifiestos solo
  tienen referencias (`externalsecret.yaml`); `secret.example.yaml` muestra la forma, sin valores.
- **Rotación:**
  - Claves de cifrado: generar una con `gen-secrets`, agregarla al principio de
    `DATA_ENCRYPTION_KEYS` (las anteriores quedan detrás) y desplegar. Lo nuevo se cifra con ella
    y lo viejo se sigue leyendo. El mantenimiento recifra en segundo plano, por lotes, las
    direcciones, los mensajes y los tokens guardados con las anteriores; los contactos que
    escriben se recifran además en cada mensaje. La vuelta que confirma el final espera una hora
    desde el cambio (`KEY_ROTATION_GRACE_MINUTES`) para alcanzar lo que escriban los procesos que
    todavía tenían la configuración anterior. El avance se ve en el log (`key_rotation_done`) y en
    la métrica `iamia_key_rotation_rows_total`. La clave vieja se retira recién cuando
    `python -m iamia key-usage` muestra que ya nada la usa (si no, esos datos no se podrían leer).
    En Kubernetes, con despliegue gradual, conviene agregarla en dos pasos: primero al final (todos
    los pods la aprenden y siguen cifrando con la anterior) y, terminado ese despliegue, al
    principio. Así ningún pod viejo encuentra un dato cifrado con una clave que no conoce.
  - `PSEUDONYMIZATION_KEY`: cambiarla rompe la búsqueda de contactos existentes; solo ante un
    compromiso, con un plan de recálculo.
  - Credenciales de canales: cambiarlas en la plataforma y en Vault; reiniciar la API (firmas) o
    el dispatcher (envíos) según corresponda.
- Grafana tiene su contraseña de administrador en `.env.observability` (separado del `.env` de la
  app) y solo escucha en `127.0.0.1`.

## Separación de entornos

- `APP_ENV` distingue desarrollo, staging y producción. El token de desarrollo de la consola solo
  funciona en desarrollo, y en producción el arranque se niega con contraseñas de ejemplo o
  configuración incompleta.
- Kubernetes tiene un overlay por entorno (`staging`, `production`), cada uno en su propio clúster
  o namespace y con sus propios secretos en Vault. Ningún entorno comparte claves de cifrado,
  credenciales de canales ni base de datos con otro.
- Las pruebas de punta a punta generan claves nuevas en cada máquina (`tests/e2e/.env.e2e`, fuera
  de Git) y usan simuladores, nunca cuentas reales.

## Gestión de incidentes

1. **Detectar.** Las alertas (con su [runbook](../manual/runbooks.md)) avisan errores, colas,
   webhooks rechazados, fallas de entrega y datos que no se pueden descifrar. La auditoría de la
   consola registra quién leyó o cambió qué.
2. **Contener.** Según el caso: deshabilitar un canal (`CHANNELS_ENABLED`) o la consola, revocar
   el acceso de una persona en el proveedor de identidad, rotar las credenciales de un canal o las
   claves de cifrado (procedimientos arriba), o bajar a cero las réplicas de un rol.
3. **Investigar.** Logs en Loki (30 días), trazas en Tempo (7 días) y la auditoría (365 días).
   Ninguno tiene texto de mensajes ni direcciones, así que se pueden compartir con el equipo que
   investiga.
4. **Comunicar.** Si hay datos personales comprometidos, avisar al responsable de la base de datos
   y al área legal, que define las comunicaciones a las personas afectadas y a la autoridad de
   control (AAIP).
5. **Cerrar.** Registrar causa, impacto y corrección, y sumar una prueba o una alerta para que no
   se repita.

## Riesgos residuales y pendientes

- **Detección de manipulación por reglas.** Frena los intentos conocidos y el resto de las capas
  limitan el daño, pero no es infalible. Recomendado: un clasificador dedicado (por ejemplo, un
  modelo de guardia local) y pruebas de red teaming periódicas.
- **Sin mTLS entre servicios.** El tráfico interno se autentica con credenciales por servicio y
  está restringido por NetworkPolicy, pero no va cifrado (salvo PostgreSQL, que CloudNativePG
  sirve con TLS). Un service mesh (Linkerd, Istio) agregaría cifrado e identidad de servicio.
- **Redis sin réplica ni persistencia.** Es solo caché e idempotencia rápida; si se cae, la
  idempotencia la cubre PostgreSQL y la caché se reconstruye.
- **Plataformas de terceros.** Los mensajes de WhatsApp, Messenger, Instagram, X, Telegram y SMS
  pasan por esas empresas. Es inherente a atender en esos canales y debe informarse a la persona.
- **Calidad de las respuestas.** El asistente responde solo con la base de conocimiento, pero la
  base hay que mantenerla: una FAQ desactualizada produce una respuesta desactualizada.
