# Glosario

| Término | Significado |
|---|---|
| AAIP | Agencia de Acceso a la Información Pública: autoridad de control de la Ley 25.326 de protección de datos personales |
| AES-256-GCM | algoritmo de cifrado simétrico con clave de 256 bits que además detecta si el dato cifrado fue alterado |
| API | interfaz que un programa ofrece a otros para pedirle cosas por la red |
| API Gateway | punto de entrada único a la plataforma: recibe los pedidos de afuera, aplica controles comunes (TLS, límites, autenticación) y los dirige al servicio que corresponde. En este proyecto lo forman nginx (o el ingress en Kubernetes) y la API |
| Base vectorial | base de datos que guarda embeddings y encuentra los más parecidos a uno dado; aquí, PostgreSQL con pgvector |
| BSUID | identificador de usuario que WhatsApp asigna a cada persona por empresa, como alternativa al número de teléfono |
| Circuit breaker | protección que, ante fallas repetidas de un servicio, deja de llamarlo por un tiempo para no empeorar la situación, y vuelve a probar después |
| Cola quorum | tipo de cola de RabbitMQ replicada en varios nodos, que no pierde mensajes si se cae uno |
| Contrapresión | mecanismo por el que un sistema saturado frena la entrada en lugar de caerse; aquí, las colas tienen tope y la API responde que reintenten |
| DLQ (cola de descarte) | cola donde quedan los trabajos que agotaron sus reintentos, para revisarlos sin bloquear al resto |
| Docker y Docker Compose | Docker empaqueta cada programa con lo que necesita en un contenedor; Compose levanta varios contenedores juntos con un archivo de configuración |
| Embedding | vector de números que representa el significado de un texto; textos parecidos tienen vectores cercanos |
| FP8 y cuantización | representar los pesos de un modelo con menos bits (FP8 usa 8) para que ocupe menos memoria y responda más rápido, con una pérdida mínima de calidad |
| GPU | procesador gráfico; hace en paralelo las operaciones que necesitan los modelos de IA y por eso los acelera |
| Grafana | herramienta para armar tableros con métricas, logs y trazas |
| HMAC | firma calculada con una clave secreta; permite comprobar que un mensaje viene de quien tiene la clave y no fue modificado |
| HPA | Horizontal Pod Autoscaler: agrega o quita réplicas de un servicio en Kubernetes según su uso de CPU u otra métrica |
| Idempotencia | propiedad de una operación que, repetida, produce el mismo resultado que una sola vez; evita respuestas duplicadas cuando una plataforma reenvía un mensaje |
| Inferencia | usar un modelo ya entrenado para producir una respuesta |
| Inferencia distribuida | repartir un modelo entre varias GPU (por ejemplo, con tensor parallelism, que divide cada capa entre ellas) o servir réplicas del modelo en varios servidores |
| JWT | token firmado que lleva la identidad y los roles de quien lo presenta; la API verifica la firma en cada pedido |
| KEDA | componente de Kubernetes que escala los servicios según métricas externas, como la cantidad de mensajes en una cola |
| Kubernetes | plataforma que ejecuta contenedores en varios servidores, los reinicia si fallan, los reparte y los escala |
| KV cache (caché de atención) | memoria de la GPU donde el modelo guarda lo ya procesado de cada pedido; define cuántos pedidos entran a la vez |
| Ley 25.326 | ley argentina de protección de los datos personales |
| Ley de Little | relación entre llegadas, tiempo de atención y trabajos en curso: en promedio, trabajos en curso = llegadas por segundo × segundos por trabajo |
| LLM | modelo de lenguaje de gran tamaño: genera texto a partir de una instrucción y un contexto |
| Loki | sistema que guarda y consulta los logs |
| N+1 | tener un equipo más de los necesarios, para que la caída de uno no reduzca la capacidad |
| NetworkPolicy | regla de Kubernetes que define qué componentes pueden hablar con cuáles |
| OAuth 2.0 y OIDC | OAuth 2.0 es el estándar para autorizar el acceso a una API con tokens; OIDC (OpenID Connect) le suma la identidad de la persona y es lo que usa la consola |
| OpenTelemetry | estándar para generar y enviar trazas, métricas y logs |
| Outbox y sweeper | patrón por el que todo se guarda primero en la base y después se publica en la cola; el sweeper republica lo que haya quedado sin publicar |
| Pod | la unidad mínima que ejecuta Kubernetes: uno o más contenedores que corren juntos |
| Prometheus | sistema que recolecta métricas y evalúa las reglas de alerta |
| Prompt injection | intento de manipular al modelo con instrucciones escondidas en el mensaje, para que ignore sus reglas |
| RabbitMQ | servidor de colas de mensajes; reparte los trabajos entre los procesos |
| RAG | generación aumentada por recuperación: antes de responder, se buscan los fragmentos relevantes de la base de conocimiento y el modelo responde solo con ellos |
| RBAC | control de acceso basado en roles: cada rol tiene un conjunto de permisos |
| Redis | base de datos en memoria, muy rápida; aquí se usa para cachés, locks y límites |
| RPO y RTO | RPO es cuántos datos se pueden perder como máximo ante un desastre (medido en tiempo); RTO, cuánto se tarda en volver a funcionar |
| Runbook | procedimiento escrito para responder a una alerta concreta |
| Seudonimización | reemplazar un dato que identifica a alguien por un valor que no lo identifica sin una clave aparte |
| Single-flight | técnica por la que, si llegan muchas consultas iguales a la vez, se calcula una sola respuesta y el resto la espera |
| Token | unidad de texto con la que trabaja un modelo; en español, una palabra son en promedio uno o dos tokens |
| vLLM | servidor de inferencia de código abierto para modelos de lenguaje en GPU, con agrupado continuo de pedidos |
| VRAM | memoria de la GPU; tiene que alcanzar para los pesos del modelo y la caché de atención |
| Webhook | aviso que una plataforma manda a una dirección de la plataforma propia cada vez que pasa algo, por ejemplo, cuando llega un mensaje |
| Zero Trust | modelo de seguridad en el que nada se considera confiable por estar dentro de la red: cada acceso se autentica y se autoriza |
