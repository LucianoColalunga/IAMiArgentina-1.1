# 0006. Modelos en servidores propios, detrás de una API compatible con OpenAI

- Estado: aceptada
- Fecha: 2026-11

## Contexto

Las consultas pueden traer datos personales aunque se pida no mandarlos. Enviarlas a una API de
IA de terceros implicaría una cesión o transferencia internacional de datos (Ley 25.326, arts. 11
y 12) y una dependencia de un proveedor externo para un servicio de atención.

## Decisión

- El modelo de lenguaje y el de embeddings corren en servidores propios con vLLM. En CPU (demo y
  pruebas) se usa llama.cpp con la misma API.
- La aplicación habla con ambos por la API `/v1` compatible con OpenAI (`LLM_BASE_URL`,
  `EMBEDDINGS_BASE_URL`): cambiar de modelo o de servidor es configuración, no código.
- Modelo por defecto en producción: `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` (mezcla de expertos:
  unos 30.000 millones de parámetros en total y unos 3.300 millones activos por token, en FP8;
  entra en una GPU de 48 GB o más y genera con un costo por token cercano al de un modelo chico).
  Embeddings: `BAAI/bge-m3` (1024 dimensiones, multilingüe).
- Los modelos se descargan una vez a un volumen y los servidores arrancan sin salida a internet
  (`HF_HUB_OFFLINE=1`).
- Cualquier modelo nuevo se evalúa con el mismo conjunto de preguntas y con las pruebas de
  manipulación antes de cambiarlo (la versión del modelo forma parte de la clave de caché).

## Alternativas consideradas

- **API comercial (OpenAI, Anthropic, Google).** Mejor calidad sin hardware propio, pero con
  cesión de datos y dependencia externa. Queda como opción si el área legal la habilita con
  contrato y anonimización previa.
- **Llama 3.1 8B (el de la versión 1).** Funciona y sigue sirviendo para la demo en CPU. Para
  producción se eligió un modelo más grande con costo por token parecido; la elección final se
  tiene que confirmar con el conjunto de preguntas propio y revisión humana antes de abrir el
  servicio.

## Consecuencias

- Hace falta hardware con GPU (ver el plan de hardware) y alguien que lo opere.
- Las respuestas siempre se arman con lo recuperado de la base oficial: cambiar de modelo no
  cambia la fuente de la información.
