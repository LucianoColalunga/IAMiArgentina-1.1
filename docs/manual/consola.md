# Consola de operadores

La consola es la parte del sistema que usan las personas: toman las conversaciones que el
asistente deriva, responden por el mismo canal por el que escribió el ciudadano, revisan los
envíos que fallaron y consultan la auditoría. Está en `/console/` (en Kubernetes, en su propio
host detrás del login OIDC).

## Acceso y roles

Se entra con la cuenta de la organización (OIDC: Keycloak, Entra ID u otro). En producción el
proxy de login (`oauth2-proxy`) agrega el token a cada pedido y la consola no guarda nada en el
navegador. En una instalación de prueba se puede pegar un token generado con
`python -m iamia dev-token` (solo funciona con `APP_ENV=development`); vive en `sessionStorage`
y se borra al cerrar la pestaña.

Los permisos dependen del rol que asigna el proveedor de identidad (se aceptan `admin` o
`iamia-admin`, y lo mismo con el resto):

| Puede... | operator | supervisor | auditor | admin |
|---|:-:|:-:|:-:|:-:|
| Ver conversaciones y su historial | ✓ | ✓ | | ✓ |
| Tomar, responder, devolver y cerrar | ✓ | ✓ | | ✓ |
| Reasignar conversaciones de otra persona | | ✓ | | ✓ |
| Ver el estado de los canales | | ✓ | ✓ | ✓ |
| Ver la auditoría | | ✓ | ✓ | ✓ |
| Reintentar envíos fallidos | | | | ✓ |
| Borrar los datos de un contacto | | | | ✓ |

El auditor revisa la trazabilidad pero no lee conversaciones: separar esas funciones es parte
del cumplimiento de la Ley 25.326 (acceso a datos personales solo cuando hace falta).

## Conversaciones

La lista muestra las conversaciones filtradas por estado:

- **Esperando persona**: el asistente las derivó (la persona pidió hablar con alguien, o el
  asistente no encontró información dos veces seguidas). Son las que hay que atender primero.
- **Con operador/a**: alguien ya las tomó.
- **Asistente**: las que está respondiendo el asistente. Se pueden mirar sin intervenir.
- **Cerradas**: terminadas o vencidas.

Las dos primeras están por orden de llegada: arriba, la que más espera. Asistente y Cerradas
muestran las 50 con actividad más reciente.

Cada conversación muestra el canal, la dirección del contacto **enmascarada** (por ejemplo
`*********1234` o `m***@gmail.com`) y desde cuándo espera. La dirección completa nunca aparece en
la consola ni en los logs.

### Atender una conversación

1. Abrirla y tocar **Tomar conversación**. Queda asignada a quien la tomó; si otra persona
   intenta tomarla, la consola le avisa que ya está tomada.
2. Leer el historial. Los datos personales que la persona escribió (DNI, CUIL, teléfono, correo,
   tarjeta) aparecen reemplazados por marcadores como `[DNI]`: el sistema los enmascara antes de
   guardarlos.
3. Escribir la respuesta y **Enviar respuesta**. Sale por el mismo canal y queda en el historial
   con la marca de operador.
4. Al terminar: **Devolver al asistente** (si la persona puede seguir con el bot) o **Cerrar**.

Si nadie la toma, a las 4 horas (`HANDOFF_TIMEOUT_MINUTES`) el sistema le avisa a la persona que
no hay operadores disponibles y que puede seguir con el asistente o volver a pedir atención más
tarde.

### Límites de cada canal que conviene conocer

- **WhatsApp**: solo se puede escribir libremente dentro de las 24 h posteriores al último
  mensaje de la persona. Pasado ese plazo el envío falla como "ventana cerrada", salvo que esté
  configurada una plantilla aprobada de recontacto.
- **Messenger e Instagram**: fuera de las 24 h, una persona (no el bot) puede responder hasta 7
  días después, si la función Human Agent está aprobada por Meta.
- **X**: 15 mensajes cada 15 minutos para toda la cuenta. Las respuestas pueden tardar en salir
  cuando hay demanda.
- **SMS**: cada respuesta se recorta a 3 segmentos; las tildes se reemplazan para no encarecer el
  envío.
- **Correo**: la respuesta sale en el mismo hilo.

## Problemas de envío

Lista los mensajes que no se pudieron entregar con el error que devolvió la plataforma (por
ejemplo `permanent:21211`, número inválido en SMS, o `window_closed`). Un administrador puede
**reintentar** un envío cuando la causa está resuelta; el reintento queda auditado. Los errores
transitorios no aparecen acá: el sistema los reintenta solo.

## Canales

Canales habilitados, cuántos mensajes esperan en la cola de cada uno (y en la de entrada) y un
resumen de las últimas 24 horas. Sirve para responder rápido "¿está saliendo WhatsApp?" sin
entrar a Grafana; el estado de los circuitos y las latencias están en los tableros.

## Borrar los datos de un contacto

Cuando una persona pide la supresión de sus datos (derecho de supresión, Ley 25.326, art. 16),
un administrador abre su conversación y usa **Borrar datos del contacto**. Se eliminan el
contacto, sus conversaciones y sus mensajes; queda solo el registro de auditoría de que se
borró, sin datos personales. No se puede deshacer.

La persona también puede pedirlo sola escribiendo `BORRAR` en cualquier canal: el asistente le
pide confirmación (`BORRAR CONFIRMAR`) y el borrado se hace en la siguiente pasada de
mantenimiento.

## Auditoría

Registro de quién hizo qué y cuándo: tomar, responder, devolver, cerrar, reintentar, borrar y
cada vez que alguien abre el detalle de una conversación (una vez cada 10 minutos por persona y
conversación, para que el registro no se llene de repeticiones). El registro no guarda textos ni
direcciones, solo identificadores. Se conserva 365 días (`RETENTION_AUDIT_DAYS`).

## Comandos que puede usar la persona

Funcionan en todos los canales, en mayúsculas o minúsculas:

| Comando | Qué hace |
|---|---|
| `AYUDA` | explica qué puede hacer el asistente |
| `HUMANO`, `OPERADOR` (o "quiero hablar con una persona") | deriva la conversación a la consola |
| `PRIVACIDAD` | explica qué datos se guardan y por cuánto tiempo |
| `STOP` / `BAJA` | deja de recibir mensajes del asistente |
| `ALTA` / `REANUDAR` | vuelve a recibirlos |
| `BORRAR` | pide la supresión de sus datos (hay que confirmar con `BORRAR CONFIRMAR`) |
| `VINCULAR` | genera un código para unir este canal con otro (ver abajo) |

Se reconocen solo si el mensaje entero es el comando: "¿cómo doy de baja la tarjeta?" es una
consulta, "BAJA" es el comando.

**Vinculación de canales.** Una misma persona puede usar, por ejemplo, Telegram y WhatsApp. El
sistema no las une por su cuenta: la persona escribe `VINCULAR` en uno, recibe un código de un
solo uso y lo manda desde el otro (`vincular <código>`). Recién ahí la consola muestra ambas
conversaciones como de la misma persona. Nunca se vinculan identidades por coincidencia de nombre,
teléfono u otro dato.
