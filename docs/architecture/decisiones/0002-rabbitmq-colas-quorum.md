# 0002. RabbitMQ con colas quorum para el trabajo asincrónico

- Estado: aceptada
- Fecha: 2026-11

## Contexto

Entre la API y el procesamiento hace falta una cola durable: la API guarda el mensaje, contesta
200 a la plataforma y el resto se hace después. Necesitamos reintentos con espera (un proveedor
que devuelve 429 pide esperar), una cola de descarte para lo que no se pudo procesar,
contrapresión cuando el sistema se satura y que un canal atrasado no frene a los demás.

## Decisión

RabbitMQ 4 con colas **quorum** (replicadas con Raft):

- `iamia.inbound` para el procesamiento y una cola `iamia.outbound.<canal>` por canal.
- Reintentos con espera sin plugins: colas de nivel con TTL fijo (1 s, 5 s, 30 s, 2 min, 10 min,
  30 min) que al vencer devuelven el trabajo a su cola por un exchange de reencolado.
- Colas de descarte `iamia.dead.inbound` e `iamia.dead.outbound`, y `x-delivery-limit` para
  cortar mensajes que se reentregan sin fin.
- Contrapresión con `x-max-length` y `x-overflow=reject-publish`: si la cola se llena, el broker
  rechaza la publicación y el mensaje queda guardado en PostgreSQL para que el sweeper lo
  publique después (ver [0004](0004-bandeja-de-salida-y-sweeper.md)).
- Los trabajos llevan solo identificadores; el texto nunca pasa por el broker.

## Alternativas consideradas

- **Kafka.** Excelente para flujos de eventos y retención larga, pero no tiene reintentos con
  espera ni colas de descarte nativos, y su operación es más pesada para el volumen esperado
  (cientos de mensajes por segundo, no millones).
- **Redis Streams.** Simple, pero la durabilidad depende de la persistencia de Redis, que acá se
  usa solo como caché.
- **Colas administradas de una nube (SQS, Pub/Sub).** Válidas, pero atan el despliegue a un
  proveedor y el requisito es poder correr on-premise.

## Consecuencias

- Las colas quorum toleran la caída de un nodo de tres sin perder mensajes confirmados.
- El reintento con espera queda en el broker: un worker que se reinicia no pierde la cuenta.
- En la prueba de resiliencia, el reinicio de RabbitMQ demoró las respuestas en vuelo unos 35 s
  (lo que tarda el sweeper en republicar lo que no se confirmó), sin pérdidas ni duplicados.
