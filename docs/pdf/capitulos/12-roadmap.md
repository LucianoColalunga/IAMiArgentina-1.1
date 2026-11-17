# Roadmap

La versión 1.1 deja la plataforma lista para un piloto: funciona de punta a punta con los ocho
canales simulados, pasó todas las pruebas y tiene documentados la infraestructura, la seguridad y
la operación. Lo que falta para atender a personas reales no se resuelve desde el código: cuentas
de las plataformas, hardware, proveedor de identidad, base de conocimiento definitiva y
validaciones legales. El roadmap ordena ese camino en etapas, cada una con un criterio claro para
pasar a la siguiente.

## Etapa 1: piloto

**Objetivo:** atender a un grupo acotado de personas por uno o dos canales y medir.

- Instalar el servidor con 2 GPU (configuración inicial del capítulo 5) y medir el modelo de
  producción con `vllm bench serve`. Reemplazar con esa medición los supuestos de capacidad.
- Habilitar el chat web y un canal de mensajería (Telegram es el más rápido de aprobar;
  WhatsApp, el de más alcance).
- Dar de alta la consola en el proveedor de identidad, con sus roles, y cargar los secretos en el
  gestor de secretos.
- Cargar la base de conocimiento definitiva, con un responsable y una fecha de revisión para cada
  contenido.
- Armar un conjunto de preguntas reales con respuestas revisadas por personas y medir la calidad.
- Inscribir la base de datos en el Registro Nacional de Bases de Datos y aprobar el texto de
  `PRIVACIDAD` con el área legal.
- Prueba de penetración y red teaming del asistente antes de abrirlo.

**Para pasar a la etapa 2:** respuestas correctas y con su fuente en al menos el 90 % del
conjunto de control, ningún hallazgo crítico o alto abierto, y la operación (alertas, consola,
actualización de la base) funcionando con el equipo que la va a sostener.

## Etapa 2: producción

**Objetivo:** abrir el servicio al público con alta disponibilidad.

- Instalar el clúster de Kubernetes (3 nodos de aplicación y 3 de datos) con los operadores, y el
  servidor de GPU completo más uno de reserva.
- Repetir en staging las pruebas de punta a punta, de carga y de resiliencia, ahora con modelos
  reales y en nodos separados.
- Integración continua: construir, probar, escanear (gitleaks, Trivy, pip-audit) y firmar las
  imágenes en cada cambio, y fijar su digest en el overlay de producción.
- Habilitar los demás canales a medida que cada plataforma los apruebe.
- Ensayar la restauración de un respaldo y el paso al sitio de contingencia, y medir RPO y RTO
  reales.
- Guardia con los runbooks de cada alerta y protección contra ataques volumétricos delante del
  ingress.

**Para pasar a la etapa 3:** tres meses en producción dentro de los objetivos de disponibilidad y
de tiempo de respuesta, y un simulacro de contingencia aprobado.

## Etapa 3: escala nacional

**Objetivo:** crecer hacia un millón de personas por día y 100.000 conversaciones simultáneas.

- Sumar servidores de GPU según la demanda medida (configuraciones 3 a 5 del capítulo 5).
- Particionar la tabla de mensajes por fecha y sumar réplicas de lectura para la consola y los
  informes.
- Réplica de Redis con conmutación automática si la caché se vuelve crítica para los tiempos.
- Cifrado e identidad entre servicios con un service mesh (mTLS).
- Ajustar la autoescala (HPA y KEDA) con los datos de tráfico real.

## Mejoras del asistente

Estas mejoras se pueden sumar en cualquier etapa, sin cambiar la arquitectura:

| Mejora | Qué aporta | Cómo |
|---|---|---|
| Clasificador de manipulación | detectar intentos que las reglas no ven | un modelo de guardia local delante del motor, con las reglas actuales como primera barrera |
| Ajuste fino del modelo (entrenamiento) | respuestas con el tono y el vocabulario del servicio, menos instrucciones en cada pedido | ajuste fino con LoRA a partir de preguntas y respuestas revisadas por personas y anonimizadas, respetando la Ley 25.326; la base de conocimiento sigue siendo la fuente de verdad, así que el ajuste no reemplaza al RAG |
| Evaluación continua | detectar a tiempo si la calidad baja | correr el conjunto de control en cada cambio de modelo, de prompt o de base, y revisar una muestra de conversaciones reales |
| Preguntas sin respuesta | saber qué falta en la base | informe periódico de las consultas sin información para que el área de contenido las complete |
| Mensajes de voz | atender a quien prefiere hablar | transcripción con un modelo local, sin enviar el audio a terceros |
| Más idiomas | atender en otras lenguas | el modelo y los embeddings ya son multilingües; falta la base de conocimiento traducida |

## Riesgos del roadmap

| Riesgo | Mitigación |
|---|---|
| Demoras en la aprobación de las plataformas | empezar el trámite de cada canal en la etapa 1 y abrir primero los que no requieren aprobación (chat web, Telegram) |
| Disponibilidad de GPU | el diseño admite varias GPU de la lista de compatibilidad del servidor (capítulo 5) |
| Base de conocimiento desactualizada | responsables y fechas de revisión por contenido; informe de preguntas sin respuesta |
| Demanda mayor que la prevista | la cola absorbe los picos sin perder mensajes y la plataforma escala por nodos |
