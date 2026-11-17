# Cambios

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones con
[versionado semántico](https://semver.org/lang/es/).

## [1.1.0] - 2026-11-16

Primera versión pensada para atender a escala nacional. Parte de la prueba de concepto v1 (ver
[auditoría de la v1](docs/reports/auditoria-v1.md)).

### Agregado

- Atención por ocho canales: WhatsApp, Telegram, Instagram, Messenger, mensajes directos de X,
  chat web, correo y SMS. Webhooks verificados con la firma de cada plataforma.
- Gestor de conversaciones: contexto de los últimos turnos, comandos (`AYUDA`, `HUMANO`,
  `PRIVACIDAD`, `BORRAR`, `VINCULAR`, `BAJA`, `ALTA`), bienvenida a los saludos y derivación a una
  persona cuando la piden o cuando el asistente no encuentra información dos veces seguidas.
- Vinculación de identidades entre canales solo a pedido de la persona, con código de un solo uso.
- Consola de operadores con login OIDC, cuatro roles y auditoría de cada acción.
- Procesamiento asincrónico: RabbitMQ con colas quorum, reintentos con espera y cola de descarte;
  bandeja de salida con sweeper; idempotencia en Redis y PostgreSQL.
- PostgreSQL con pgvector para los datos, la base de conocimiento (versionada, cambio azul/verde)
  y la caché semántica.
- Cifrado por campo con AES-256-GCM, seudonimización con HMAC y rotación de claves con recifrado
  en segundo plano; comando `key-usage` para confirmar que una clave vieja ya no se usa.
- Retención de datos y borrado a pedido (`BORRAR` o desde la consola).
- Observabilidad: métricas de Prometheus, tres tableros de Grafana (aplicación, motor de IA e
  infraestructura con GPU), alertas con su runbook, logs JSON sin datos personales y trazas de
  OpenTelemetry con el id del mensaje.
- Manifiestos de Kubernetes (kustomize, CloudNativePG, RabbitMQ, KEDA, HPA, PodDisruptionBudget,
  NetworkPolicy, External Secrets, oauth2-proxy) con overlays de staging y producción.
- Modelo por defecto Qwen3-30B-A3B-Instruct-2507-FP8 en vLLM; llama.cpp en CPU para demostración.
- Pruebas unitarias, de integración contra servicios reales, de punta a punta con simuladores de
  las ocho plataformas, de carga y de resiliencia ([resultados](docs/reports/resultados-pruebas.md)).
- Documentación de arquitectura con registro de decisiones, plan de hardware y capacidad,
  seguridad, canales, consola, runbooks, despliegue y documentación técnica en PDF.

### Cambiado respecto de la v1

- ChromaDB reemplazado por pgvector; Llama 3.1 8B por Qwen3-30B-A3B en FP8.
- Redis, PostgreSQL y RabbitMQ con contraseña aunque estén en la red interna.
- `POST /v1/chat` mantiene el mismo contrato.

### Corregido antes de publicar (encontrado en las pruebas)

- Un trabajo que fallaba sin explicación se reentregaba en bucle: ahora vuelve con espera y
  termina en la cola de descarte.
- Una caída de la base dejaba un mensaje reservado tres minutos: la reserva es transaccional y se
  libera ante un error.
- La API contestaba 500 mientras PostgreSQL se reiniciaba: ahora 503 con `Retry-After`.
- nginx seguía usando la IP vieja de la API después de recrearla: resuelve el nombre en cada
  momento.
- Un "Hola" solo recibía el aviso de fuera de tema: ahora recibe la bienvenida.
- En el correo, la respuesta no seguía el asunto del último mensaje.
- En la consola, las pestañas Asistente y Cerradas mostraban las conversaciones más viejas: ahora
  las más recientes, con un índice para no ordenar la tabla entera.
- La instrucción para generar las claves levantaba la base con la contraseña de ejemplo.
- En una instalación nueva, `key-usage` mostraba la rotación "en curso" durante la primera hora.
- La ingesta seguía una redirección antes de comprobar que el destino fuera un dominio oficial:
  ahora valida cada pedido antes de hacerlo.
- Imágenes: la de producción no incluye pip y aplica los parches de Debian; la de PostgreSQL corre
  sin root.
