# Conclusiones

IAMiArgentina 1.1 transforma una prueba de concepto (un endpoint de consulta y una página de
demostración) en una plataforma de atención multicanal pensada para operar a escala nacional. El
trabajo se apoyó en pocas decisiones de fondo, sostenidas en todo el diseño:

- **Un núcleo común y un adaptador por canal.** Las ocho plataformas entran y salen por
  adaptadores que hablan el idioma de cada una; el gestor de conversaciones y el motor de
  respuesta no saben de qué canal viene un mensaje. Sumar un canal no toca el núcleo.
- **Recibir rápido, procesar en colas.** La API solo valida, guarda y encola, así que un pico no la
  tumba; los workers y los dispatchers procesan a su ritmo y la cola absorbe la diferencia. Las
  pruebas lo confirmaron: con la PC saturada, ningún mensaje se perdió ni se duplicó.
- **No perder nada ante una falla.** Bandeja de salida con sweeper, idempotencia en dos niveles,
  reintentos con espera creciente y cola de descarte. Con cada componente reiniciado bajo carga,
  los 1.500 mensajes de la prueba se respondieron.
- **Respuestas apoyadas en fuentes.** El modelo responde solo con lo que recupera de la base de
  conocimiento y dice cuando no sabe. Los modelos corren en servidores propios: las consultas no
  salen a servicios de IA de terceros.
- **Seguridad y datos personales desde el diseño.** Firmas en cada webhook, OIDC y roles en la
  consola, cifrado y seudonimización de los datos, enmascarado antes del modelo, plazos de
  conservación y supresión a pedido, en línea con la Ley 25.326.
- **Operable.** Métricas, tableros, 16 alertas con su procedimiento, logs y trazas por mensaje, y
  una consola para que las personas atiendan lo que el asistente deriva.

La plataforma está lista para un piloto. Los números de este documento salen de pruebas reales:
784 unitarias, 45 de integración y 41 de punta a punta, carga, resiliencia, rotación de claves y
modelos reales en CPU. Lo que no se pudo probar está identificado: los canales contra las
plataformas reales, el modelo en GPU, el clúster de Kubernetes y el proveedor de identidad
dependen de cuentas e infraestructura que este entorno no tiene.

El camino a producción (capítulo 12) empieza por medir el modelo en la GPU elegida, cargar la
base de conocimiento definitiva con responsables que la mantengan y validar la calidad con
preguntas reales y revisión humana. Con eso, la arquitectura permite crecer por etapas, sumando
servidores a medida que la demanda lo pida, sin rehacer lo construido.
