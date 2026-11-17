# Auditoría final

Versión 1.1.0, 16 de noviembre de 2026. Revisión de lo que se entrega: código, imágenes,
manifiestos, configuración y documentación. Qué requisito se cumple y cómo se verificó está en el
[checklist de calidad](checklist-qa.md); los números de las pruebas, en
[resultados de las pruebas](resultados-pruebas.md).

## Conclusión

La plataforma funciona de punta a punta con los 8 canales simulados y pasó las pruebas
unitarias, de integración, de punta a punta, de carga, de resiliencia y el ensayo de rotación de
claves. Los escaneos no encontraron secretos, ni vulnerabilidades en las dependencias de Python, ni
hallazgos críticos o altos en los manifiestos de Kubernetes. Quedan avisos en paquetes del sistema
de la imagen base que todavía no tienen arreglo publicado, aceptados con las mitigaciones que se
detallan abajo.

Está lista para un piloto. Para producción falta lo que no se puede resolver desde el código:
cuentas y aprobaciones de cada plataforma, la GPU, el proveedor de identidad, el gestor de secretos,
la inscripción de la base de datos y las pruebas con personas reales (ver
[Pendientes antes de producción](#pendientes-antes-de-producción)).

## Alcance y método

- Se revisaron los archivos que se publican en el repositorio: los que excluye `.gitignore`
  (entornos `.env`, claves de desarrollo, resultados locales, entornos virtuales) no forman parte
  de la entrega y se verificó que no se versionen.
- Herramientas: gitleaks 8.30.1 (secretos), Trivy 0.76.1 (dependencias, secretos, configuración e
  imágenes; base de vulnerabilidades del 16/11/2026), pip-audit 2.10.1 (dependencias de Python),
  ruff 0.17.5 (calidad del código) y las suites de prueba del repositorio.
- Los manifiestos de Kubernetes se analizaron ya renderizados con kustomize (staging y producción,
  82 recursos cada uno), que es lo que se aplicaría al clúster. Analizar los parches sueltos da
  falsos positivos: son fragmentos que se combinan con la base.
- Las imágenes analizadas son las construidas con el código final.

## Escaneos

| Qué | Herramienta | Resultado |
|---|---|---|
| Archivos del repositorio | gitleaks | sin secretos |
| Archivos del repositorio | Trivy fs | 0 vulnerabilidades, 0 secretos |
| Dependencias de Python: los 85 paquetes del entorno de desarrollo y las versiones fijadas en `requirements.txt` | pip-audit | sin vulnerabilidades conocidas |
| Manifiestos renderizados de staging y producción | Trivy config | 0 críticos, 0 altos; 11 medios y 2 bajos en cada uno (abajo) |
| Imagen `iamia/backend:1.1.0` (Debian 13.7) | Trivy image | 0 secretos; 0 en paquetes de Python; 209 en paquetes del sistema (47 altos, 92 medios, 65 bajos, 5 sin clasificar), ninguno con arreglo publicado |
| Imagen `iamia/postgres:16-pgvector` (Alpine 3.24.4) | Trivy image | 0 |
| Código Python | ruff (lint y formato) | sin observaciones |

**Revisión final, 17 de noviembre de 2026.** Después de sumar la documentación en PDF y sus
fuentes (`docs/pdf`) se repitió la revisión sobre los 359 archivos que se publican y, después del
commit, sobre el historial de Git:

| Qué | Herramienta | Resultado |
|---|---|---|
| Secretos en los archivos y en el historial | detect-secrets 1.5.0 y reglas propias (claves privadas, tokens de Meta, GitHub, Telegram, Twilio, AWS y Google, JWT firmados, URLs con contraseña, correos y datos personales) | sin credenciales reales: las coincidencias son valores de las pruebas, referencias a secretos de Kubernetes, marcadores `CHANGE_ME` y direcciones de ejemplo |
| Dependencias de Python: el entorno completo (90 paquetes) y `backend/requirements.txt`, `backend/requirements-dev.txt` y `docs/pdf/requirements.txt` | pip-audit 2.10.1 | sin vulnerabilidades conocidas |
| Código de `docs/pdf` | ruff (lint y formato) | sin observaciones |

gitleaks y Trivy no se repitieron en esta revisión porque Docker no estaba disponible en el equipo
(poco espacio libre en el disco del sistema). El código de la aplicación, sus dependencias y las
imágenes no cambiaron desde el escaneo de la tabla anterior.

**Hallazgos en los manifiestos:**

| Id | Severidad | Cantidad | Qué es | Decisión |
|---|---|---|---|---|
| KSV-0125 | media | 10 | imágenes de registros que Trivy no tiene en su lista de confianza | aceptado: la imagen propia sale del registro de la organización (`registry.example.org` es el nombre de ejemplo) y las de terceros están fijadas por versión, y la de vLLM además por digest. En producción conviene replicarlas en el registro interno y exigirlo con una política de admisión |
| KSV-01010 | media | 1 | "ConfigMap con contenido sensible" | falso positivo: lo dispara el nombre de variables como `SMTP_PORT`, `METRICS_PORT` o `WHATSAPP_PHONE_NUMBER_ID`. Ningún secreto va en ConfigMaps: llegan por External Secrets |
| KSV-0020, KSV-0021 | baja | 1 y 1 | Redis corre con uid y gid 999, no mayores a 10000 | aceptado: es el usuario de la imagen oficial; no es root, no tiene capabilities y su NetworkPolicy solo admite a la aplicación |

**Avisos de la imagen del backend.** Son de paquetes de la base Debian 13 (`libc6`, `libsqlite3-0`,
`libsystemd0`, `libgcc`, entre otros) para los que Debian todavía no publicó arreglo. La imagen
aplica las actualizaciones de seguridad de Debian al construirse, así que basta reconstruirla cuando
salgan. Mitigaciones: el proceso corre sin root (uid 10001), con el sistema de archivos de solo
lectura, sin capabilities y con `no-new-privileges`; la imagen no tiene pip, compiladores, curl
ni wget, y la red está cerrada por NetworkPolicy. Ninguno de esos paquetes se expone a la red:
el único servicio de la imagen es la aplicación.

**Otros avisos de configuración (Dockerfiles).** DS-0026, sin `HEALTHCHECK` en las imágenes: las
comprobaciones de salud están en Compose y en las sondas de Kubernetes. DS-0013, un `cd` dentro de
un `RUN`: está en la etapa de compilación de pgvector, que no llega a la imagen final.

## Qué se corrigió durante el desarrollo

Encontrado por las pruebas o por la revisión, y corregido antes de publicar:

| Problema | Cómo se encontró | Corrección |
|---|---|---|
| Un trabajo que fallaba sin explicación se reentregaba en bucle | prueba de resiliencia | vuelve con espera creciente y termina en la cola de descarte ([ADR 0007](../architecture/decisiones/0007-reintentos-de-fallas-no-previstas.md)) |
| Una caída de PostgreSQL dejaba un mensaje reservado tres minutos | prueba de resiliencia | la reserva es transaccional y se libera ante un error |
| La API contestaba 500 mientras PostgreSQL se reiniciaba | prueba de resiliencia | 503 con `Retry-After`: la plataforma reintenta |
| nginx seguía usando la IP vieja de la API después de recrearla | prueba de resiliencia | resuelve el nombre del servicio en cada momento |
| La instrucción para generar claves levantaba PostgreSQL con la contraseña de ejemplo | revisión de la documentación | `--no-deps` en todas las instrucciones |
| La imagen de producción incluía pip y la de PostgreSQL corría como root | Trivy y revisión de imágenes | etapa final sin pip y con los parches de Debian; PostgreSQL sin root y sin `gosu` |
| La ingesta seguía una redirección antes de comprobar que el destino fuera oficial | revisión de seguridad (SSRF) | cada pedido, redirecciones incluidas, se valida antes de salir; en Kubernetes la salida a internet excluye además las direcciones link-local y compartidas |
| La consola no mostraba las conversaciones nuevas en un entorno con historia | ensayo de rotación de claves | orden por actividad reciente, con índice |
| En una instalación nueva, `key-usage` mostraba la rotación en curso durante una hora | ensayo de rotación de claves | sin margen de espera cuando hay una sola clave |
| Un "Hola" recibía el aviso de fuera de tema | prueba con modelos reales en CPU | respuesta de bienvenida a los saludos |

## Riesgos aceptados

| Riesgo | Mitigación |
|---|---|
| Avisos sin arreglo en paquetes de la imagen base | ver arriba; reconstruir la imagen ante cada actualización de Debian y repetir el escaneo en cada versión |
| Detección de manipulación del asistente basada en reglas | varias capas: relevancia mínima, delimitadores, control de salida, anclaje en el contexto y enlaces solo a dominios permitidos. Mejora prevista: un clasificador local dedicado |
| Tráfico interno sin cifrar (sin mTLS) | NetworkPolicy que niega todo por defecto, credenciales por servicio, PostgreSQL con TLS en Kubernetes. Mejora prevista: service mesh |
| Redis sin réplica ni persistencia | es solo caché e idempotencia rápida: si se pierde, PostgreSQL cubre la idempotencia y la caché se reconstruye |
| Los mensajes de cada canal pasan por la empresa dueña de la plataforma | inherente a atender por esos canales; se informa a la persona con `PRIVACIDAD` |
| Imágenes de terceros en registros públicos | versiones fijadas (vLLM por digest); replicarlas en el registro interno antes de producción |

## Pendientes antes de producción

No se pueden resolver desde el código o necesitan infraestructura que este entorno no tiene:

1. **Cuentas y aprobaciones de cada plataforma:** WhatsApp Business (verificación de la empresa,
   número y plantillas), revisión de la app de Meta para Instagram y Messenger, plan de la API de X
   con mensajes directos, número de SMS y dominio de correo con SPF y DKIM. Detalle en
   [canales](../integrations/README.md). Todos los canales se probaron con simuladores fieles a
   cada API, no contra las plataformas reales.
2. **Modelo en la GPU elegida:** medir con `vllm bench serve` y una prueba de carga de punta a punta
   con modelos reales. La imagen de vLLM está fijada pero no se ejecutó en este entorno, que no
   tiene GPU. Reemplazar con esa medición los supuestos del [plan de hardware](../hardware/README.md).
3. **Calidad de las respuestas:** un conjunto de preguntas reales con revisión humana y la tasa
   real de aciertos de la caché.
4. **Base de conocimiento definitiva,** con un responsable y fecha de revisión de cada contenido.
5. **Kubernetes:** los manifiestos se renderizan y se analizaron, pero no se aplicaron a un clúster
   real. Falta instalarlos en staging con los operadores (CloudNativePG, RabbitMQ, KEDA, External
   Secrets, cert-manager) y repetir allí las pruebas de punta a punta y de resiliencia.
6. **Proveedor de identidad y gestor de secretos:** dar de alta la aplicación en el IdP (roles de
   la consola) y cargar los secretos en Vault.
7. **Datos personales:** inscribir la base en el Registro Nacional de Bases de Datos (Ley 25.326,
   art. 21) y que el área legal apruebe el texto de `PRIVACIDAD`.
8. **Seguridad ofensiva:** prueba de penetración y red teaming del asistente antes de abrirlo al
   público.
9. **Contingencia:** ensayar la restauración de un respaldo y el paso al sitio de contingencia, y
   medir RPO y RTO reales.
10. **Integración continua:** un pipeline que construya, pruebe, escanee (gitleaks, Trivy,
    pip-audit) y firme las imágenes en cada cambio, y fije su digest en el overlay de producción.
11. **Volumen:** con el tráfico real, evaluar particionar la tabla de mensajes por fecha.

## Integridad del trabajo

- El repositorio de la v1 no se modificó: se clonó aparte, solo para leerlo. Sus ramas siguen en
  el mismo commit que en GitHub y la copia local no tiene cambios.
- La 1.1 es un repositorio nuevo con su propio historial, sin relación con el de la v1.
- No hay secretos reales en el repositorio: los `.env` de ejemplo no tienen valores y las claves
  de prueba se generan en cada máquina y quedan fuera de Git.
