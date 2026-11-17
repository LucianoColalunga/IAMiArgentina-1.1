# 0001. Monolito modular con roles en lugar de microservicios

- Estado: aceptada
- Fecha: 2026-11

## Contexto

La versión 1 era una API sincrónica de una sola pieza: recibía la pregunta, llamaba al modelo y
devolvía la respuesta en el mismo pedido. Para atender ocho canales hace falta separar la recepción
de los webhooks (tiene que contestar en milisegundos), el procesamiento con IA (tarda segundos y
depende de GPU) y el envío por cada plataforma (tiene sus propios límites y caídas).

El equipo que mantenga esto va a ser chico. Diez servicios con repositorios, versiones y contratos
propios serían más trabajo de operación que de producto.

## Decisión

Un solo paquete de Python (`iamia`) y una sola imagen de contenedor, con roles que se eligen al
arrancar:

| Rol | Qué hace | Cómo escala |
|---|---|---|
| `api` | webhooks, chat web, `/v1/chat`, consola | HPA por CPU |
| `worker` | conversación + motor RAG | KEDA por largo de la cola de entrada |
| `dispatcher` | envío por canal | réplicas por grupo de canales (WhatsApp aparte) |
| `maintenance` | sweeper, retención, borrados, derivaciones vencidas | una réplica |
| `migrate`, `ingest` | tareas de una vez | Job / CronJob |

Los módulos tienen límites claros: `channels` (contratos), `integrations/<canal>` (adaptadores),
`conversations` (dominio, sin base de datos ni HTTP), `ai` y `rag` (motor), `persistence`,
`messaging` y `security`. El dominio depende de puertos (`conversations/ports.py`), no de las
implementaciones.

## Alternativas consideradas

- **Microservicios por canal y por etapa.** Más aislamiento de despliegue, pero cada canal es
  poco código (entre 50 y 350 líneas por adaptador). El aislamiento que importa —que un canal caído
  no frene al resto— se logra con colas, circuit breakers y throttles por canal dentro del
  dispatcher.
- **Seguir con la API sincrónica de la v1.** No resiste los reintentos de las plataformas ni los
  picos: un webhook que tarda lo que tarda el modelo termina en reintentos duplicados.

## Consecuencias

- Una sola versión que probar, construir y escanear. Las pruebas de punta a punta levantan la
  plataforma completa con un `docker compose`.
- Los roles se escalan por separado aunque compartan imagen.
- Si en el futuro un módulo necesita otro ritmo de despliegue (por ejemplo, un canal con mucho
  cambio), el límite ya está trazado: se puede sacar a otro proceso sin reescribir el dominio.
