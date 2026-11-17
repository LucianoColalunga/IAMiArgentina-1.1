# 0005. Las identidades de distintos canales solo se vinculan si la persona lo pide

- Estado: aceptada
- Fecha: 2026-11

## Contexto

Una misma persona puede escribir por WhatsApp y por el chat web. Unir esas conversaciones ayuda al
operador, pero hacerlo por coincidencias (mismo nombre, un teléfono que aparece en un mensaje)
puede mezclar a dos personas distintas y exponer datos de una a la otra. Además, el sistema no
tiene forma de verificar que un nombre de usuario de Instagram corresponde a un número de
teléfono.

## Decisión

- Cada contacto es la dirección de un canal (`contacts`). Una persona (`persons`) agrupa
  contactos solo cuando ella misma lo pide.
- El flujo: la persona escribe `VINCULAR` en un canal y recibe un código de un solo uso con
  vencimiento; lo manda desde el otro canal y recién ahí se unen.
- La búsqueda de contactos usa HMAC de la dirección con una clave aparte: ni siquiera con acceso a
  la base se puede buscar a alguien por su teléfono sin esa clave.

## Alternativas consideradas

- **Unir por teléfono o correo encontrados en el texto.** Rápido, pero un error expone la
  conversación de otra persona.
- **No unir nunca.** Más simple, pero el operador pierde el contexto cuando la persona cambia de
  canal.

## Consecuencias

- Cero uniones accidentales: los contactos solo se unen con el código. La prueba de punta a punta
  recorre el flujo completo (código pedido por Telegram, usado desde WhatsApp, rechazado al
  reusarlo desde Messenger) y verifica en la consola que la conversación figura como vinculada.
- La persona puede pedir el borrado (`BORRAR`) y se eliminan todos sus contactos vinculados.
