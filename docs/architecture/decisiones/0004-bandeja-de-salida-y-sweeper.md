# 0004. Guardar primero, publicar después: bandeja de salida, leases y sweeper

- Estado: aceptada
- Fecha: 2026-11

## Contexto

Las plataformas reintentan los webhooks que no reciben 200 a tiempo y a veces mandan el mismo
evento dos veces. Del lado nuestro, cualquier proceso se puede caer entre guardar un mensaje y
publicarlo en la cola, o entre reclamarlo y terminarlo. Necesitamos que ningún mensaje se pierda y
que nadie reciba la misma respuesta dos veces.

## Decisión

Entrega **al menos una vez** con **idempotencia** en cada paso:

1. **Entrada.** La API corta duplicados rápido con Redis (`SET NX`) y guarda el mensaje en
   PostgreSQL con un índice único `(canal, id del proveedor)`. Recién después publica el
   trabajo. Si la cola no está disponible, contesta 200 igual: el mensaje ya está guardado.
2. **Bandeja de salida.** Cada mensaje guardado tiene `enqueued_at`. El sweeper republica los que
   no se marcaron como encolados después de 30 s.
3. **Leases.** Un worker reclama un mensaje (estado `processing` con vencimiento) dentro de una
   transacción. Si el worker muere, el lease vence y el sweeper lo libera. Completar exige seguir
   siendo el dueño: un worker lento no pisa al que lo tomó después.
4. **Salida.** Las respuestas largas guardan qué partes ya se enviaron, para que un reintento no
   repita las que llegaron.

## Alternativas consideradas

- **Publicar antes de guardar.** Si la base falla después de publicar, el worker procesa algo
  que no existe.
- **Transacciones distribuidas entre la base y el broker.** Complejas y con soporte pobre en
  RabbitMQ.
- **Exactly-once.** No existe de punta a punta con plataformas externas que reintentan: lo
  práctico es al menos una vez con deduplicación.

## Consecuencias

- En la prueba de resiliencia (reinicios de worker, dispatcher, RabbitMQ, Redis y PostgreSQL y
  recreación de la API con 10 mensajes/s) se respondieron los 1500 mensajes, sin duplicados ni
  pérdidas.
- El costo es una ventana de demora: lo que queda sin confirmar durante un reinicio del broker
  espera al sweeper (30 s más el intervalo de barrido).
