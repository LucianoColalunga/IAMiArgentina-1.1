# Checklist de calidad

Cada requisito de la versión 1.1, su estado y cómo se verificó. Estados:

- **Cumplido**: implementado y verificado con pruebas o con una revisión que se puede repetir.
- **Sin probar en real**: implementado y probado con simuladores o en forma local. Falta la prueba
  contra la plataforma o la infraestructura real, que requiere cuentas, hardware o aprobaciones de
  terceros.
- **Pendiente**: no se puede resolver desde el código; está en la
  [auditoría final](auditoria-final.md#pendientes-antes-de-producción).

## Control de calidad final

| Verificación | Estado | Cómo se verificó |
|---|---|---|
| El repositorio anterior sigue intacto | Cumplido | el clon de la v1 no tiene cambios (`git status`) y sus ramas apuntan al mismo commit que en GitHub (`git ls-remote`) |
| El repositorio nuevo es independiente | Cumplido | historial propio que empieza en la 1.1, sin commits ni remotos de la v1 |
| El único contribuyente visible es Luciano Colalunga | Cumplido | `git log --format='%an %ae %cn %ce'` muestra solo ese autor; ningún commit tiene coautores ni atribuciones a herramientas |
| El README cumple las restricciones | Cumplido | aviso de proyecto independiente y no oficial, sin logos ni afirmaciones institucionales, sin secretos ni detalles que faciliten ataques, sin otros contribuyentes; descripción técnica |
| La arquitectura multicanal está implementada | Cumplido | 8 adaptadores con un contrato común; ida y vuelta por cada canal en las pruebas de punta a punta |
| Las integraciones están documentadas | Cumplido | [canales](../integrations/README.md): cuenta, variables, webhook, límites, costos y requisitos de cada plataforma |
| Las funcionalidades no probadas están identificadas | Cumplido | estado "Sin probar en real" en este documento y pendientes de la auditoría final |
| Las pruebas se ejecutaron | Cumplido | 784 unitarias, 45 de integración, 41 de punta a punta, carga, estrés, resiliencia y rotación de claves ([resultados](resultados-pruebas.md)) |
| No hay secretos publicados | Cumplido | gitleaks y Trivy sin hallazgos sobre los archivos; revisión final con detect-secrets y reglas propias sobre los archivos que se publican y sobre el historial de Git, sin credenciales reales ni datos personales; `.env*` y claves de prueba fuera de Git |
| El despliegue es reproducible | Cumplido | versiones fijas de dependencias e imágenes; `scripts/e2e.sh` construye y prueba todo desde cero; los overlays de Kubernetes se renderizan sin errores |
| La documentación está completa | Cumplido | arquitectura y decisiones, hardware, seguridad, canales, consola, runbooks, despliegue, Kubernetes, observabilidad e informes |
| El PDF se generó y se abre | Cumplido | [documentación técnica](../IAMiArgentina-1.1-Documentacion-Tecnica.pdf), 70 páginas generadas con `docs/pdf/generar.py`; se renderizaron todas las páginas y se extrajo su texto, y el índice y los marcadores llevan a la página correcta |
| Los diagramas coinciden con la implementación | Cumplido | los componentes de las figuras son los servicios de `docker-compose.yml` y los recursos de los manifiestos de Kubernetes, con los nombres del código; el modelo de datos tiene las tablas de las migraciones; lo externo (plataformas, balanceador, Vault, almacenamiento de objetos, sitio de contingencia) se dibuja aparte |

## Canales

| Canal | Qué se implementó | Estado |
|---|---|---|
| WhatsApp (Cloud API) | webhooks con firma, recepción, envío, estados de entrega, plantilla para retomar fuera de las 24 h, BSUID, errores, reintentos, idempotencia, procesamiento asincrónico, derivación; "leído" y "escribiendo" | Sin probar en real: falta la cuenta de WhatsApp Business verificada |
| Instagram | webhooks con firma, mensajes directos, envío en partes de 1000 bytes, ventana de 24 h y etiqueta HUMAN_AGENT para personas | Sin probar en real: falta la revisión de la app de Meta |
| Messenger | integración con la página, webhooks, respuestas, sesiones por conversación, derivación a operadores | Sin probar en real: ídem |
| Telegram | webhook con token secreto, recepción, envío, comandos, solo chats privados; documentos, fotos y audios registrados por tipo | Sin probar en real: falta crear el bot |
| X | mensajes directos por X Activity API, CRC, OAuth 2.0 con PKCE y renovación automática, límite de 15 cada 15 minutos con cola | Sin probar en real: falta el plan de la API y la aprobación de X para respuestas con IA |
| Chat web | página propia, sesiones firmadas, long polling, CSP estricta | Cumplido |
| Correo | entrada firmada desde el servidor de correo, salida por SMTP en el mismo hilo, filtro de respuestas automáticas | Sin probar en real: falta el servidor de correo y SPF/DKIM |
| SMS | webhook de Twilio con firma, GSM-7, máximo 3 segmentos, estados | Sin probar en real: falta el número y el registro del remitente |
| Nuevos canales | contrato común: un adaptador nuevo no toca el motor ni la conversación | Cumplido: el motor no importa ningún adaptador |

Costos, límites y permisos de cada plataforma están en [canales](../integrations/README.md).

## Conversaciones

| Requisito | Estado | Cómo |
|---|---|---|
| Canal, usuario, sesiones y contexto | Cumplido | contacto por canal con dirección cifrada; conversación con estado e hilo; últimos turnos como contexto |
| Estados, mensajes pendientes, errores y reintentos | Cumplido | estados de mensaje y de entrega; reintentos con espera y cola de descarte; consola de envíos fallidos |
| Derivación y atención humana | Cumplido | `HUMANO` o derivación automática; consola con tomar, responder, devolver y cerrar |
| Trazabilidad | Cumplido | auditoría de la consola, trazas por mensaje, logs con id |
| Sin mezclar identidades | Cumplido | vinculación solo con código de un solo uso pedido por la persona ([ADR 0005](../architecture/decisiones/0005-vinculacion-explicita-de-identidades.md)) |
| Consola administrativa | Cumplido | cuatro roles con OIDC; probada con token de desarrollo. Sin probar en real con el proveedor de identidad |

## Escala, alta disponibilidad y resiliencia

| Requisito | Estado | Cómo |
|---|---|---|
| Escenarios de 100.000 a 1.000.000 de usuarios y de 10.000 a 100.000 simultáneos | Cumplido (diseño) | [plan de hardware](../hardware/README.md#los-seis-escenarios-de-diseño), con los supuestos marcados |
| Monolito modular o microservicios | Cumplido | monolito modular con roles separables ([ADR 0001](../architecture/decisiones/0001-monolito-modular.md)) |
| Colas (Kafka o RabbitMQ) | Cumplido | RabbitMQ con colas quorum ([ADR 0002](../architecture/decisiones/0002-rabbitmq-colas-quorum.md)) |
| Contrapresión, circuit breakers, timeouts, reintentos, DLQ, idempotencia, single-flight, cachés distribuidas | Cumplido | probados en unitarias, integración y resiliencia |
| La caída de un canal no afecta a los otros | Cumplido | colas y circuit breaker por canal; prueba de punta a punta |
| Balanceo, réplicas, autoescalado, failover | Sin probar en real | manifiestos con HPA, KEDA, PDB, CloudNativePG y RabbitMQ en 3 nodos; no se aplicaron a un clúster real |
| Recuperación ante desastres | Pendiente | diseño y procedimiento documentados; falta el simulacro |

## Hardware e inteligencia artificial

| Requisito | Estado | Cómo |
|---|---|---|
| Plan para el Gigabyte G493-ZB3-AAP1: configuraciones inicial, intermedia y máxima, varios servidores, redundancia, energía, refrigeración y red | Cumplido (diseño) | [plan de hardware](../hardware/README.md) con la ficha y la lista de GPU validadas del fabricante |
| vLLM, paralelismo, cuantización, VRAM, caché de prefijos, lotes, embeddings | Cumplido (diseño y configuración) | parámetros en los manifiestos; sin GPU disponible para medir |
| Auditoría de las tecnologías de la v1 | Cumplido | [auditoría de la v1](auditoria-v1.md#tecnologías-conservar-actualizar-o-reemplazar) |
| Motor, gestión de modelos, RAG, embeddings, recuperación semántica | Cumplido | probado con modelos reales en CPU; en GPU, sin probar en real |
| Caché exacta y semántica | Cumplido | prueba de punta a punta y con modelos reales |
| Control de contexto y de alucinaciones | Cumplido | presupuesto de tokens, umbral de relevancia, anclaje de la respuesta y enlaces permitidos |
| Versionado y reindexación sin cortes | Cumplido | versiones de la base y cambio azul/verde, probados en integración |
| Protección contra prompt injection | Cumplido | reglas, delimitadores, control de salida y anclaje; pruebas unitarias y de punta a punta. Red teaming pendiente |
| Cambiar de modelo sin reconstruir la plataforma | Cumplido | el modelo es configuración (`LLM_BASE_URL`, `LLM_MODEL`) |

## Seguridad y datos personales

| Requisito | Estado | Cómo |
|---|---|---|
| Análisis de amenazas | Cumplido | [modelo de amenazas](../security/README.md#modelo-de-amenazas) por superficie |
| OAuth 2.0, RBAC, sesiones | Cumplido | OIDC con JWT verificado en la API, cuatro roles, sesiones con vencimiento |
| Secretos y rotación | Cumplido | Vault con External Secrets; rotación de claves de cifrado probada de punta a punta; tokens de X que se renuevan solos |
| Cifrado | Cumplido | AES-256-GCM por campo, HMAC para buscar; TLS en el borde y en PostgreSQL |
| Webhooks y repetición | Cumplido | firma de cada plataforma, idempotencia; webhooks falsificados rechazados en 7 canales en las pruebas |
| Rate limiting y DDoS | Cumplido, con límite | límites por IP, contacto y cliente; un ataque volumétrico necesita protección del proveedor de red |
| SQL injection, XSS, CSRF, SSRF, exposición de información | Cumplido | [ataques web comunes](../security/README.md#ataques-web-comunes) |
| Auditoría | Cumplido | cada acción y lectura en la consola |
| Mínimo privilegio y Zero Trust | Cumplido | NetworkPolicy que niega todo por defecto, credenciales por servicio, JWT verificado aunque haya proxy, contenedores sin root ni capabilities |
| Ley 25.326 | Cumplido en el diseño | minimización, información, seguridad, confidencialidad, supresión, plazos. Pendiente: inscripción de la base y texto legal |
| Entornos, logs seguros, anonimización, retención | Cumplido | `APP_ENV` y overlays; logs sin datos personales; enmascarado y seudónimos; retención automática |
| Backups, recuperación e incidentes | Cumplido en procedimiento | respaldos documentados y continuos en Kubernetes; [gestión de incidentes](../security/README.md#gestión-de-incidentes). Falta ensayar una restauración |
| Sin secretos en Git | Cumplido | gitleaks |

## Calidad del código

| Requisito | Estado | Cómo |
|---|---|---|
| SOLID, contratos e interfaces, inyección de dependencias | Cumplido | protocolos por dominio (canales, mensajería, conversación, modelo, embeddings); dependencias armadas en un solo lugar (`bootstrap.py`) |
| Separación por dominios | Cumplido | `channels`, `conversations`, `ai`, `rag`, `messaging`, `persistence`, `security`, `integrations` |
| Integraciones aisladas; el motor no depende de ninguna plataforma | Cumplido | ningún módulo de `ai`, `rag` o `conversations` importa un adaptador |
| Tipado, validaciones y excepciones centralizadas | Cumplido | anotaciones de tipos, Pydantic, manejadores de error únicos en la API, ruff sin observaciones |
| Archivos acotados | Cumplido | 117 módulos y 11.675 líneas de Python; el más largo tiene 450 |
| Todo testeable | Cumplido | 784 pruebas unitarias sin servicios externos |

## Observabilidad

| Requisito | Estado | Cómo |
|---|---|---|
| Prometheus, Grafana, OpenTelemetry, Loki, Alertmanager | Cumplido | `compose.observability.yml` y manifiestos de Kubernetes |
| Latencia, colas, errores, disponibilidad, mensajes por canal, RAG, tiempos de respuesta, inferencia | Cumplido | tableros "Visión general" y "Motor de IA"; 19 reglas con su runbook |
| CPU, RAM, GPU, VRAM | Cumplido, GPU sin probar en real | tablero "Infraestructura y GPU"; los paneles de GPU necesitan el GPU Operator |
| Health checks, readiness y liveness | Cumplido | `/health/live` y `/health/ready`; sondas en todos los Deployments |

## Pruebas

| Tipo | Estado | Resultado |
|---|---|---|
| Unitarias | Cumplido | 784 pasan |
| Integración | Cumplido | 45 pasan contra PostgreSQL, Redis y RabbitMQ reales |
| Punta a punta | Cumplido | 41 pasan con la plataforma completa y simuladores de las 8 plataformas |
| Seguridad | Cumplido | pruebas de firmas, manipulación, datos personales y permisos; escaneos sin hallazgos críticos. Prueba de penetración pendiente |
| Carga | Cumplido | 2.400 de 2.400 a 20/s; 10.049 de 10.049 escalonada |
| Estrés | Cumplido | 9.000 de 9.000 a 150/s, por encima de la capacidad del equipo de prueba: nada se pierde |
| Resiliencia | Cumplido | 1.500 de 1.500 con reinicios de cada componente |
| Con modelos reales | Cumplido en CPU, pendiente en GPU | 10 de 10 respuestas con su fuente en CPU |
