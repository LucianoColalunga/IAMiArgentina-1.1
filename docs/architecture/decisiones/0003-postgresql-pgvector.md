# 0003. PostgreSQL con pgvector como base de datos y base vectorial

- Estado: aceptada
- Fecha: 2026-11

## Contexto

La versión 1 usaba ChromaDB para los vectores y no guardaba conversaciones. La 1.1 necesita
guardar contactos, conversaciones, mensajes con sus estados, auditoría y tokens OAuth, con
transacciones e índices únicos para la idempotencia. Tener además una base vectorial aparte
significa otro servicio que respaldar, asegurar (ChromaDB corría sin autenticación en la red
interna) y mantener en alta disponibilidad.

## Decisión

PostgreSQL 16 con la extensión pgvector para todo:

- Datos transaccionales: `persons`, `contacts`, `conversations`, `messages`, `audit_log`,
  `link_codes`, `oauth_tokens`.
- Base de conocimiento versionada: cada ingesta crea `kb_chunks_<versión>` con índice HNSW, se
  valida y recién entonces se cambia el puntero `kb_active` en una transacción (azul/verde). La
  versión anterior queda para volver atrás.
- Caché semántica en una tabla con índice HNSW y vencimiento.
- En Kubernetes, CloudNativePG: tres instancias, failover automático, PgBouncer como pooler y
  backups continuos (WAL) a almacenamiento de objetos.

## Alternativas consideradas

- **Seguir con ChromaDB.** Sin transacciones con el resto de los datos, sin autenticación en la
  versión usada y con un camino de alta disponibilidad menos maduro.
- **Una base vectorial dedicada (Qdrant, Milvus).** Mejor rendimiento con cientos de millones de
  vectores, pero la base de conocimiento de trámites tiene miles de fragmentos: pgvector con HNSW
  responde en milisegundos a ese tamaño.

## Consecuencias

- Un solo motor que respaldar, monitorear y asegurar. Las restricciones únicas de PostgreSQL son
  la garantía real contra duplicados.
- La reindexación no corta el servicio y se puede revertir.
- Si la base de conocimiento creciera varios órdenes de magnitud, el contrato `rag/models.py`
  permite cambiar el almacén vectorial sin tocar el motor.
