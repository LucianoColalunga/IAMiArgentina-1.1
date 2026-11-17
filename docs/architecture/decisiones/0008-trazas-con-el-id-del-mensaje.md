# 0008. Trazas de OpenTelemetry con el id del mensaje como id de traza

- Estado: aceptada
- Fecha: 2026-11

## Contexto

Un mensaje pasa por tres procesos (API, worker, dispatcher) conectados por una cola. Las trazas
distribuidas suelen propagar el contexto en cabeceras, pero los trabajos de la cola llevan solo
identificadores y el sweeper puede republicar un mensaje mucho después, desde otro proceso. Los
logs ya llevan un `trace_id` por mensaje (el `X-Request-ID` del webhook).

## Decisión

- Cada proceso abre su span raíz con un id de traza igual al `trace_id` del mensaje (32
  caracteres hexadecimales). No hace falta propagar contexto: la API, el worker y el dispatcher
  suman sus spans a la misma traza aunque el trabajo haya pasado por el sweeper.
- El muestreo depende solo del id de traza (`TraceIdRatioBased`): todos los procesos guardan o
  descartan los mismos mensajes. Por defecto 100 % en Compose y 10 % en Kubernetes.
- Los spans llevan canal, ids internos, resultado y tipo de error. Nunca texto, direcciones ni el
  mensaje de una excepción (podría traer datos personales).
- Es opcional: con `OTEL_EXPORTER_OTLP_ENDPOINT` vacío no se configura el SDK y los spans no
  cuestan nada.

## Alternativas consideradas

- **Propagar `traceparent` en el trabajo de la cola.** Es lo estándar, pero se pierde cuando el
  sweeper republica desde la base y obliga a guardar el contexto junto al mensaje.
- **Solo logs.** Ya existían, pero no muestran dónde se va el tiempo dentro del motor (embedding,
  recuperación, modelo).

## Consecuencias

- Desde una línea de log en Loki se salta a la traza en Tempo con el mismo id.
- La prueba de punta a punta `test_observability.py` manda un webhook con un id conocido y
  verifica que llegan los spans de los tres procesos, anidados y sin datos personales.
