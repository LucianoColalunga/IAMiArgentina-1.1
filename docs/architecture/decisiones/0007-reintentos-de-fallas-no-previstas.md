# 0007. Un trabajo que falla sin explicación vuelve con espera y termina en la cola de descarte

- Estado: aceptada
- Fecha: 2026-11

## Contexto

La primera corrida de la prueba de resiliencia mostró tres problemas con PostgreSQL reiniciándose:

- Un trabajo cuyo handler fallaba volvía a la cola en el acto y se reintentaba en un bucle
  (22 errores en un segundo para el mismo mensaje).
- Un mensaje quedó tomado (`processing`) hasta que venció su lease: la respuesta tardó 210 s.
- La API contestó 500 mientras la base no respondía.

## Decisión

- Una excepción no prevista en el handler no reencola en el acto: el trabajo vuelve por los
  niveles de espera (1 s, 5 s, 30 s, 2 min, 10 min) y, agotados, va a la cola de descarte
  (`messaging/failures.py`).
- El worker libera el mensaje que tenía tomado antes de propagar la falla, así el reintento no
  espera a que venza el lease.
- Reclamar un mensaje se hace dentro de una transacción.
- Las fallas de infraestructura que se resuelven solas (conexión rechazada, base reiniciándose,
  Redis caído) se reconocen (`core/transient.py`): la API contesta 503 con `Retry-After: 5` y los
  logs las registran como aviso, sin traza completa.

## Consecuencias

- Segunda corrida, con los mismos reinicios y además la recreación de la API: respuesta máxima
  35 s (antes 210 s), sin respuestas 500 y sin duplicados.
- Las plataformas que reciben 503 reintentan solas (Meta y Telegram lo hacen), así que el
  mensaje no se pierde aunque la base esté caída en el momento del webhook.
