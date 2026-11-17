# Motor de IA

## Modelos

| Uso | Producción (GPU) | Demostración (CPU) |
|---|---|---|
| Modelo de lenguaje | `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` en vLLM v0.33.0 | Llama 3.2 3B Instruct (Q4_K_M) en llama.cpp |
| Embeddings | `BAAI/bge-m3`, 1024 dimensiones, en vLLM | bge-m3 (Q8_0) en llama.cpp |

**Por qué Qwen3-30B-A3B.** Es un modelo de mezcla de expertos: tiene 30.000 millones de parámetros
pero activa unos 3.300 millones por token, así que cuesta parecido a un modelo denso de 8.000
millones y responde mejor. En FP8 ocupa unos 31 GB y entra en una sola GPU de 48 GB o más, con
lugar para la caché de atención. Maneja bien el español y sigue instrucciones con precisión, que es
lo que importa para responder solo con el contexto recuperado.

**Por qué bge-m3.** Embeddings multilingües de 1024 dimensiones con buen rendimiento en español; ya
se usaba en la versión 1.

**Modelos detrás de una API estándar.** El motor habla con los dos modelos a través de una API
compatible con la de OpenAI (`/v1/chat/completions` y `/v1/embeddings`). vLLM y llama.cpp exponen
esa misma API, así que cambiar de servidor o de modelo es configuración (`LLM_BASE_URL`,
`LLM_MODEL`, `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_MODEL`), no código. Las consultas nunca salen a
servicios de IA de terceros.

## Inferencia con vLLM

vLLM es el servidor de inferencia de producción. La imagen está fijada por versión y por digest y
corre sin acceso a internet (`HF_HUB_OFFLINE=1`): los pesos se cargan desde un volumen precargado.

| Parámetro | Valor | Para qué |
|---|---|---|
| `--max-model-len` | 8192 | largo máximo de entrada más salida; un pedido típico usa unos 1.300 tokens |
| `--gpu-memory-utilization` | 0.90 | lo que no ocupan los pesos queda para la caché de atención |
| `--enable-prefix-caching` | activado | las instrucciones del sistema son iguales en todos los pedidos: se calculan una vez |
| `--kv-cache-dtype fp8` | opcional | duplica los pedidos que entran a la vez, con pérdida mínima |
| `--max-num-seqs` | según la medición | tope de pedidos simultáneos por réplica |
| `--tensor-parallel-size` | 1 | el modelo entra en una GPU; con uno más grande se reparte entre dos GPU del mismo switch PCIe |

vLLM agrupa los pedidos de forma continua (continuous batching): cada pedido nuevo se suma al lote
en curso sin esperar a que termine el anterior, así que la GPU trabaja llena. También expone
métricas de cola, uso de la caché de atención, tokens por segundo y tiempo hasta el primer token,
que se ven en el tablero de infraestructura (capítulo 9).

Del lado de la aplicación, cada llamada al modelo tiene controles propios:

| Control | Valor por defecto |
|---|---|
| Timeout de la generación | 60 s |
| Llamadas concurrentes al modelo por proceso | 32; la que no consigue lugar en 10 s se reintenta más tarde |
| Circuit breaker | se abre con 5 fallas seguidas y prueba de nuevo a los 30 s |
| Temperatura | 0,1 (respuestas estables y apegadas al contexto) |
| Tokens de salida | hasta 512 |
| Presupuesto por cliente | 100.000 tokens por día |

## RAG: respuestas con recuperación

RAG (generación aumentada por recuperación) significa que el modelo no responde de memoria: antes
de generar, el motor busca en la base de conocimiento los fragmentos más parecidos a la pregunta y
se los pasa como contexto, con la instrucción de responder solo con eso. Si la base no tiene
información suficiente, el asistente lo dice y ofrece hablar con una persona.

[[figura:motor|Pasos del motor de respuesta, de la consulta a la respuesta]]

El motor (`ai/engine.py`) está ordenado para cortar lo antes posible y no gastar GPU de más: los
controles baratos van primero y la generación al final. Una consulta repetida sale de la caché
exacta en milisegundos; una manipulación evidente se corta sin llamar al modelo.

| Parámetro | Valor | Efecto |
|---|---|---|
| `RETRIEVAL_TOP_K` | 4 | fragmentos que se recuperan por consulta |
| `MIN_RELEVANCE_SCORE` | 0,52 | por debajo, la consulta no tiene información en la base o está fuera de tema |
| `RETRIEVAL_SCORE_MARGIN` | 0,08 | se descartan los fragmentos que quedan a más de 0,08 del mejor |
| `MAX_CONTEXT_TOKENS` | 2500 | tope del contexto que va al modelo |
| `HNSW_EF_SEARCH` | 64 | precisión de la búsqueda aproximada en pgvector |
| `GROUNDING_MODE` | `observe` | el anclaje se mide y se registra; con `enforce`, una respuesta sin apoyo se bloquea |
| `GROUNDING_MIN_SCORE` | 0,35 | anclaje mínimo para considerar que la respuesta se apoya en el contexto |

## Embeddings

Un embedding es una lista de números (un vector) que representa el significado de un texto: dos
textos que dicen lo mismo con otras palabras quedan cerca. El motor calcula el embedding de cada
fragmento al indexar la base y el de cada consulta al responder, siempre con el mismo modelo, y
compara por similitud coseno.

El modelo y la dimensión quedan registrados en cada versión de la base (`kb_versions`). Si se
cambia el modelo de embeddings hay que volver a indexar: la ingesta arma una versión nueva con el
modelo nuevo y la activa recién cuando está completa. La caché semántica usa una tabla por
dimensión (`semantic_cache_d1024`), así que vectores de modelos distintos nunca se mezclan.

## Base vectorial

La base vectorial es PostgreSQL con la extensión pgvector: la misma base que guarda las
conversaciones, con transacciones, réplicas y respaldos.

- Cada versión de la base de conocimiento es una tabla `kb_chunks_<versión>` con un índice HNSW
  (`m = 16`, `ef_construction = 64`) sobre la columna `embedding`.
- La tabla `kb_active` apunta a la versión vigente. Cambiar de versión es cambiar ese puntero, en
  una transacción: las consultas en curso no se cortan y volver atrás es inmediato (azul/verde).
- Los procesos leen la versión vigente cada 30 segundos (`KB_REFRESH_SECONDS`).

## Ingesta de la base de conocimiento

| Etapa | Qué hace |
|---|---|
| Fuentes | FAQs propias en `data/faqs` (JSON y guías en Markdown) y páginas oficiales listadas en `data/sources.txt` |
| Descarga | solo páginas `https` de dominios oficiales; respeta `robots.txt`, va pausada y valida cada redirección antes de seguirla |
| Extracción | toma el contenido principal de cada página y lo separa por secciones; descarta menús, pies y scripts |
| Limpieza | quita duplicados y respuestas demasiado cortas; conserva solo enlaces `https` |
| Fragmentos | 1000 caracteres con 150 de solapamiento; cada fragmento repite el trámite y la pregunta para entenderse solo |
| Indexado | calcula los embeddings por lotes, crea la tabla y su índice HNSW y valida el resultado |
| Activación | cambia `kb_active` a la versión nueva solo si la validación pasó |

```bash
dc run --rm ingest ingest --if-changed     # vuelve a indexar si cambiaron las FAQs
dc run --rm ingest ingest --from-web       # suma las páginas oficiales de data/sources.txt
dc run --rm ingest ingest --dry-run        # muestra qué indexaría, sin escribir
dc run --rm ingest ingest --rollback       # vuelve a la versión anterior
```

`dc` es el comando de Compose de la instalación, con sus archivos y el perfil `models`. En
Kubernetes la ingesta es un CronJob semanal (domingos a las 04:00, hora argentina).

## Cachés

| Caché | Dónde | Clave | Vigencia |
|---|---|---|---|
| Exacta | Redis | pregunta normalizada más las versiones de la base, del prompt y del modelo | 24 horas; los rechazos y las respuestas sin información, 1 hora |
| Semántica | PostgreSQL (pgvector) | embedding de la consulta; acierta con similitud de 0,95 o más | 24 horas |
| Single-flight | Redis | la misma pregunta en curso | lock de 90 s; quien llega después espera hasta 20 s el resultado |

Como la clave incluye las versiones, una base nueva, un prompt nuevo o un modelo nuevo no
devuelven respuestas viejas. Si Redis falla, el motor sigue sin caché: las respuestas tardan más,
pero no se cortan.

Single-flight resuelve un caso típico de los picos: cientos de personas preguntan lo mismo a la
vez. Sin él, todas esas consultas llegarían juntas al modelo; con él, se genera una sola respuesta
y el resto la recibe de la caché.

## Contexto de la conversación

Una pregunta como "¿y cuánto sale?" no se entiende sola. Antes de consultar al motor, el gestor de
conversaciones la reescribe como pregunta completa con los últimos tres turnos (`CONTEXT_TURNS`),
usando el mismo modelo con una generación corta de hasta 80 tokens. Si la reescritura falla, se usa
el mensaje tal cual. La búsqueda y las cachés trabajan sobre la pregunta reescrita.

## Protección contra manipulación y errores

La defensa está en capas, y ninguna es la única:

- **Antes del modelo:** limpieza del texto, detección de intentos de manipulación (prompt
  injection) y umbral de relevancia, que deja afuera lo que no tiene que ver con la base.
- **En el prompt:** el contexto va delimitado de forma que el texto de la persona no puede cerrar
  la sección ni hacerse pasar por instrucciones.
- **Después del modelo:** control de salida (fuga de las instrucciones, código canario, enlaces
  solo a dominios oficiales) y anclaje: la respuesta tiene que apoyarse en los fragmentos
  recuperados.
- **Datos personales:** DNI, CUIL, teléfonos, correos, tarjetas, CBU y pasaportes se enmascaran
  antes de llegar al modelo y antes de guardarse.

Los detalles de las reglas de detección no se publican, a propósito. La detección por reglas no es
infalible; el roadmap (capítulo 12) prevé un clasificador local dedicado y pruebas de red teaming.

## Cambiar el modelo

1. Elegir el modelo y verificar que entre en la GPU con margen para la caché de atención (capítulo
   5).
2. Precargar los pesos en el volumen de modelos (en Kubernetes, el Job `vllm-model-download`).
3. Cambiar `LLM_MODEL` (y, si hace falta, los parámetros de vLLM) y desplegar. Las cachés no
   mezclan respuestas: el modelo forma parte de la clave.
4. Medir con `vllm bench serve` y con las preguntas de control antes de abrirlo a todo el tráfico.
5. Si se cambia el modelo de embeddings, volver a indexar la base (`ingest`) para que la versión
   nueva use el modelo nuevo.
