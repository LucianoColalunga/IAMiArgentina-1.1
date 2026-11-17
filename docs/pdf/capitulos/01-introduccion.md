# Introducción

## Qué es IAMiArgentina 1.1

IAMiArgentina 1.1 es una plataforma tecnológica de inteligencia artificial local, orientada a la
atención automatizada, la recuperación de información y la comunicación multicanal.

Recibe consultas escritas por ocho canales (WhatsApp, Telegram, Instagram, Messenger, mensajes
directos de X, chat web, correo electrónico y SMS), las reúne en un único gestor de conversaciones
y las responde con modelos de lenguaje que corren en servidores propios. Las respuestas salen solo
de su base de conocimiento y llevan la fuente. Cuando el asistente no puede ayudar, o la persona lo
pide, la conversación pasa a un operador desde una consola web.

> **Aviso.** Proyecto independiente. No es un servicio oficial de Mi Argentina ni del Estado
> nacional, no está vinculado a ningún organismo ni autorizado por él. Las respuestas de
> demostración salen de preguntas frecuentes públicas de argentina.gob.ar.

## Objetivo

Construir la base técnica de un servicio de atención por mensajería que pueda crecer por etapas
hasta escala nacional (cientos de miles de personas por día y picos de decenas de miles de
conversaciones a la vez) sin mandar las consultas a servicios de inteligencia artificial de
terceros y sin perder mensajes cuando falla algún componente.

## Problema que resuelve

| Problema | Cómo lo aborda la plataforma |
|---|---|
| Mucha gente pregunta lo mismo al mismo tiempo, sobre todo en los picos | respuestas automáticas con cachés: una pregunta frecuente se calcula una vez y se reutiliza |
| Cada persona escribe por el canal que ya usa | un adaptador por canal y una sola lógica de conversación para los ocho |
| Respuestas inconsistentes o inventadas | el motor responde solo con la base de conocimiento, con umbral de relevancia, control de salida y la fuente de cada respuesta |
| Privacidad de las consultas | modelos en servidores propios; textos con los datos personales enmascarados y cifrados |
| Casos que el asistente no puede resolver | derivación a una persona, a pedido o automática, con una consola para operadores |
| Picos y fallas | procesamiento asincrónico con colas: recibir no depende de la velocidad del modelo y nada se pierde si un componente se reinicia |

## Alcance de la versión 1.1

| Incluido | Fuera del alcance de esta versión |
|---|---|
| Ocho canales con sus webhooks, envíos, estados de entrega y límites | las cuentas, números y aprobaciones de cada plataforma, que otorga cada empresa |
| Gestor de conversaciones con comandos, contexto y derivación | atención por voz, imágenes o documentos (se registra el tipo, no el contenido) |
| Motor de respuesta con recuperación (RAG), cachés y controles de seguridad | la base de conocimiento definitiva y su mantenimiento |
| Consola de operadores con roles y auditoría | integración con sistemas de turnos o de expedientes |
| Despliegue con Docker Compose y manifiestos de Kubernetes | la compra e instalación de los servidores |
| Plan de hardware y de capacidad para escala nacional | la medición del modelo en la GPU elegida |
| Pruebas unitarias, de integración, de punta a punta, de carga y de resiliencia | una prueba de penetración externa y pruebas con personas reales |

## Punto de partida: la versión 1

La versión 1 fue una prueba de concepto: una API con un único endpoint de consulta
(`POST /v1/chat`) y una página de demostración, que respondía preguntas frecuentes con un modelo
local (Llama 3.1 8B servido por Ollama o vLLM), ChromaDB como base vectorial y LangChain para armar
el flujo. Tenía 37 pruebas automáticas y unas 2.300 líneas de Python.

La 1.1 conserva el enfoque (responder solo con información oficial, con umbral de relevancia y
enlaces únicamente a dominios permitidos) y el contrato de `POST /v1/chat`. Cambia casi todo lo
demás:

| Tema | Versión 1 | Versión 1.1 |
|---|---|---|
| Canales | uno: la API de consulta | ocho, más la API de consulta |
| Procesamiento | dentro del pedido HTTP | asincrónico, con colas, reintentos y cola de descarte |
| Conversaciones | una pregunta, una respuesta | contexto, comandos, derivación a personas y consola |
| Datos | sin base de datos de conversaciones | PostgreSQL con cifrado por campo y retención |
| Base vectorial | ChromaDB | pgvector, versionada con cambio azul/verde |
| Modelo | Llama 3.1 8B | Qwen3-30B-A3B en FP8 (GPU) y Llama 3.2 3B para demostración en CPU |
| Despliegue | Docker Compose | Docker Compose y Kubernetes con alta disponibilidad |
| Pruebas | 37 | 784 unitarias, 45 de integración, 41 de punta a punta, carga y resiliencia |

## Cómo está organizado este documento

| Capítulo | Contenido |
|---|---|
| 2. Arquitectura general | componentes, recorrido de un mensaje, garantías y decisiones |
| 3. Integraciones | cómo entra, se procesa y sale un mensaje en cada canal; qué está probado |
| 4. Motor de IA | modelos, inferencia, RAG, embeddings, base vectorial y cachés |
| 5. Infraestructura | servidores, GPU, capacidad, escalado y alta disponibilidad |
| 6. Seguridad | autenticación, autorización, cifrado, datos personales, secretos y auditoría |
| 7. Base de datos | entidades, relaciones, persistencia y respaldos |
| 8. Instalación paso a paso | requisitos, configuración, Docker, ejecución y verificación |
| 9. Administración | operación diaria, logs, métricas, errores y actualizaciones |
| 10. Testing | pruebas realizadas, resultados, rendimiento y limitaciones |
| 11. Estructura del código | organización del repositorio y principios de diseño |
| 12. Roadmap | próximos pasos hasta producción y después |

Al final hay conclusiones y un glosario con los términos técnicos que aparecen en el documento.

## Convenciones

- Comandos, archivos, variables y nombres de componentes van en `letra monoespaciada`.
- `<host>` es el nombre público del servicio y `<canal>` el de un canal (`whatsapp`, `telegram`,
  `instagram`, `messenger`, `x`, `webchat`, `email` o `sms`).
- Los comandos de Docker se ejecutan desde la raíz del repositorio.
- Cuando algo se probó solo con simuladores, o falta probarlo contra el servicio real, el texto lo
  dice.
