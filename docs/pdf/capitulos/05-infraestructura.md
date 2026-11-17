# Infraestructura

## Qué corre dónde

La plataforma tiene dos tipos de carga muy distintos. La aplicación (API, workers, colas, base de
datos) usa CPU común y escala agregando réplicas. La generación de respuestas usa GPU y es el
cuello de botella: por eso las cachés y el umbral de relevancia existen para no llegar al modelo
cuando no hace falta.

| Componente | Recurso | Cómo escala |
|---|---|---|
| `api`, `worker`, `dispatcher`, `maintenance` | CPU | réplicas (HPA por CPU, KEDA por largo de cola) |
| PostgreSQL con pgvector | CPU, memoria y disco NVMe | 3 instancias con failover; primario más grande en el pico |
| RabbitMQ | CPU y disco | 3 nodos con colas quorum |
| Redis | memoria | una instancia: solo cachés |
| vLLM: modelo de lenguaje | GPU | una réplica por GPU |
| vLLM: embeddings | GPU | una réplica por servidor de modelos |

## El servidor de modelos: Gigabyte G493-ZB3-AAP1

Datos de la ficha del fabricante para la revisión 3.x (placa MZB3-G41). La revisión 1.x es igual
salvo que solo acepta procesadores EPYC 9004.

| Ítem | Especificación |
|---|---|
| Formato | 4U, 448 × 176 × 880 mm |
| Procesadores | 2 × AMD EPYC serie 9005 o 9004 (socket SP5), cTDP hasta 400 W |
| Memoria | 48 ranuras DDR5 RDIMM, 12 canales por procesador; con EPYC 9005, 5200 MT/s con un DIMM por canal |
| GPU | 8 ranuras FHFL PCIe Gen5 x16 detrás de 2 switches PCIe (PEX89144), 4 por switch |
| Otras ranuras | 8 × LP Gen5 x16 detrás de los switches y 2 × LP Gen5 x16 frontales |
| Almacenamiento | 12 bahías frontales de 2,5" Gen5 NVMe/SATA/SAS-4 y 2 × M.2 internas |
| Red incluida | 2 × 1 GbE más un puerto de administración |
| Administración | BMC ASPEED AST2600 (IPMI, Redfish, KVM HTML5) |
| Fuentes | 3+1 × 3000 W 80 PLUS Titanium; 2600 W por fuente a 200-220 V |
| Ambiente | 10 a 35 °C |

## GPU

Según la lista de compatibilidad del fabricante (la misma para las revisiones 3.x y 1.x), el
servidor admite hasta 8 GPU del mismo modelo:

| GPU | Memoria | Ancho de banda | Potencia | Para este proyecto |
|---|---|---|---|---|
| NVIDIA H100 NVL | 94 GB | 3938 GB/s | 400 W | **recomendada**: FP8 nativo y mucha memoria para la caché de atención |
| NVIDIA H100 (PCIe) | 80 GB | 2000 GB/s | 350 W | buena; menos memoria y ancho de banda que la NVL |
| NVIDIA L40S | 48 GB | 864 GB/s | 350 W | **alternativa económica**: FP8 nativo, menos memoria libre por réplica |
| NVIDIA L40 / L20 | 48 GB | 864 GB/s | 300 / 350 W | similares a la L40S, con menos rendimiento o disponibilidad variable |
| NVIDIA A100 | 80 GB | 1555 GB/s | 300 W | no recomendada: sin FP8 nativo |
| AMD Instinct MI210 | 64 GB | 1638 GB/s | 300 W | no recomendada: sin FP8; el modelo en BF16 casi no deja memoria libre |

La lista no incluye H200 ni la serie Blackwell: si se quisieran usar, hay que confirmarlo con el
fabricante antes de comprar.

**Memoria por réplica.** El modelo ocupa unos 31 GB en FP8. La caché de atención usa unos 96 KB por
token (48 capas × 4 cabezas KV × 128 dimensiones × K y V × 2 bytes), la mitad con caché en FP8.
Con la utilización configurada (90 %) quedan unos 50 GB de caché en una H100 NVL, del orden de 400
pedidos en paralelo por réplica, y unos 9 GB en una L40S, del orden de 70.

[[figura:servidor|Uso propuesto de las 8 GPU de cada servidor de modelos]]

Cada servidor dedica 6 GPU al modelo de lenguaje (una réplica por GPU), 1 a embeddings y deja 1
libre para actualizar sin corte o para un modelo de guardia. Kubernetes decide en qué GPU concreta
corre cada réplica: la figura muestra el reparto, no una asignación fija.

## CPU, memoria y almacenamiento

| Componente | Recomendación por servidor de modelos |
|---|---|
| Procesadores | 2 × EPYC 9005 de 32 núcleos (por ejemplo 9355, o 9354 de la serie 9004); vLLM usa poco CPU |
| Memoria | 24 × 32 GB DDR5 RDIMM = 768 GB, un DIMM por canal para conservar los 5200 MT/s |
| Sistema | 2 × M.2 de 960 GB en RAID 1 |
| Datos | 2 × NVMe de 2,5" y 3,84 TB en RAID 1 (modelos, imágenes y logs) |
| GPU | 8 × H100 NVL (u 8 × L40S), todas del mismo modelo |
| Red | 2 × 25 GbE (una placa por switch PCIe, en bond hacia dos switches de red) más administración aislada |
| Seguridad | kit TPM 2.0, arranque seguro y BMC en una red de administración separada |

La plataforma sin GPU se midió: unos 27 ms de CPU por mensaje entre todos los componentes
(PostgreSQL 8,1; worker 6,3; dispatcher 5,7; api 3,4; RabbitMQ 2,7; Redis y nginx 1,0).

## Escenarios de capacidad

Usuarios registrados, personas conectadas, mensajes recibidos y pedidos al modelo son cosas
distintas: un usuario registrado no genera carga por sí solo. Lo que dimensiona es cuántos mensajes
llegan por segundo en el pico y cuántos necesitan al modelo.

| Supuesto | Valor | Origen |
|---|---|---|
| Mensajes por persona activa | uno por minuto | supuesto (lee, escribe) |
| Uso diario | 3 mensajes por persona; 10 % del día en la hora pico; ráfagas del doble | supuesto, peor caso |
| Parte que llega al modelo (f) | 0,5 con la caché fría, 0,3 con la caché caliente | supuesto; el resto son comandos, cachés o fuera de tema |
| Tokens generados por respuesta | 150 | medido 89 de promedio con el modelo chico, más margen |
| Tokens por segundo por GPU | 3.000 en H100 NVL; 1.200 en L40S | **supuesto conservador, a medir** con `vllm bench serve` |

| Escenario | Mensajes/s | Al modelo/s | GPU H100 NVL | Servidores (N+1) | CPU de la plataforma |
|---|---|---|---|---|---|
| 100.000 usuarios | 17 | 8 | 1 | 1 + 1 | ~0,5 núcleos |
| 500.000 usuarios | 83 | 42 | 3 | 1 + 1 | ~2,2 núcleos |
| 1.000.000 de usuarios | 167 | 83 | 5 | 1 + 1 | ~4,5 núcleos |
| 10.000 conversaciones simultáneas | 167 | 83 | 5 | 1 + 1 | ~4,5 núcleos |
| 50.000 conversaciones simultáneas | 833 | 417 | 21 | 4 + 1 | ~22 núcleos |
| 100.000 conversaciones simultáneas | 1.667 | 833 | 42 | 7 + 1 | ~45 núcleos |

GPU necesarias = mensajes por segundo × f × tokens por respuesta ÷ tokens por segundo por GPU,
redondeado para arriba; cada servidor aporta 6. Con la caché caliente, el pico de 100.000
simultáneos baja a 25 GPU (5 + 1 servidores). Con L40S hacen falta unas 2,5 veces más GPU.

Los pedidos en curso en las GPU son muchos menos que las personas conectadas (ley de Little). En el
peor escenario, 833 pedidos por segundo de unos 3 segundos cada uno son unas 2.500 generaciones a
la vez, unas 60 por GPU, lejos de las 400 que admite una réplica en una H100 NVL.

> **Importante.** Son escenarios de diseño, no rendimientos garantizados. Antes de comprar hay que
> medir el modelo en una GPU de la familia elegida y reemplazar el supuesto de tokens por segundo.
> Si el pico supera lo instalado, nada se pierde: los mensajes esperan en la cola y la demora crece
> hasta que baja la demanda.

## Configuraciones por etapa

No hace falta comprar el servidor completo de entrada: las GPU se agregan de a una, siempre del
mismo modelo.

| Etapa | Equipamiento | Capacidad aproximada |
|---|---|---|
| 1. Inicial (piloto) | un G493-ZB3-AAP1 con los dos procesadores, 24 × 16 GB y 2 GPU (modelo y embeddings); la plataforma en Docker Compose en el mismo servidor; sin alta disponibilidad | 40 mensajes/s con H100 NVL (16 con L40S): unas 200.000 personas por día |
| 2. Intermedia | el servidor completo con 8 GPU más un segundo igual de reserva (N+1); Kubernetes con 3 nodos de aplicación y 3 de datos | 240 mensajes/s: un millón de personas por día o 10.000 conversaciones simultáneas |
| 3. Máxima por servidor | 8 × H100 NVL, 2 × EPYC 9005, 768 GB, NVMe y 25 GbE | el máximo de un equipo; desde acá se crece sumando servidores |
| 4. Varios servidores | Kubernetes reparte las réplicas del modelo entre servidores y un Service distribuye los pedidos | 5 servidores para 50.000 simultáneos y 8 para 100.000, con el de reserva |
| 5. Redundancia | N+1 en modelos, 3 instancias de PostgreSQL y de RabbitMQ, fuentes en dos circuitos, RAID 1 y sitio de contingencia | la caída de un equipo no corta el servicio |

## Distribución y escalabilidad

[[figura:despliegue|Despliegue de producción en Kubernetes]]

| Rol | Objeto | Escalado | Apagado ordenado |
|---|---|---|---|
| `api` | Deployment, HPA | CPU al 60 %, de 3 a 12 réplicas | 30 s |
| `worker` | Deployment, ScaledObject | 40 mensajes en `iamia.inbound` por réplica, de 2 a 24 | 45 s |
| `dispatcher-whatsapp` | Deployment, ScaledObject | 200 mensajes en su cola por réplica, de 2 a 8 | 45 s |
| `dispatcher-general` | Deployment, ScaledObject | 100 por réplica en las demás colas de salida, de 2 a 6 | 45 s |
| `maintenance` | Deployment | 1 réplica, estrategia Recreate | 45 s |
| `migrate` | Job | antes de cada versión | |
| `ingest` | CronJob | domingos a las 04:00, sin ejecuciones superpuestas | |
| `llm`, `embeddings` | Deployment en nodos con GPU | 6 y 1 réplicas por servidor | |

- La API corre un proceso por pod y escala con pods.
- Con N servidores de modelos, un parche del overlay fija `llm` en 6 × N y `embeddings` en N. El
  máximo de workers tiene que acompañar: más workers que capacidad de vLLM solo arman cola dentro
  de vLLM.
- Con todo al máximo, la aplicación puede pedir unas 510 conexiones a PostgreSQL; PgBouncer las
  reduce a 440 conexiones de servidor, dentro del máximo de 500 de la base.
- Las réplicas se reparten por zona y por nodo, y PostgreSQL y RabbitMQ tienen anti-afinidad
  obligatoria por nodo.

## Alta disponibilidad y contingencia

| Componente | Cómo se protege |
|---|---|
| PostgreSQL | CloudNativePG con 3 instancias en nodos distintos y réplica sincrónica: si cae el primario, se promueve una réplica sin perder commits. Backups continuos (WAL cada 5 minutos como máximo) y base diaria en almacenamiento de objetos, con 30 días de retención y recuperación a un momento dado |
| RabbitMQ | 3 nodos con colas quorum: sigue funcionando con un nodo caído. No necesita backup: los mensajes están en PostgreSQL y el sweeper los republica |
| Redis | caché reconstruible; si se cae, PostgreSQL cubre la idempotencia |
| Modelos | réplicas sin estado; N+1 servidores en racks y circuitos eléctricos distintos |
| Aplicación | varias réplicas por rol, PodDisruptionBudget y apagado ordenado |

**Objetivos ante la pérdida total del clúster de base de datos:** RPO de 5 minutos o menos y RTO de
30 minutos o menos. Con un sitio de contingencia (clúster réplica de PostgreSQL y al menos un
servidor de modelos), el RTO objetivo es de menos de una hora con el sitio tibio y de menos de un
día en frío. Son objetivos a validar con un simulacro, no garantías.

## Energía, refrigeración y red

- **Consumo por servidor:** 8 × 400 W de GPU, 2 × 280 W de CPU, más memoria, discos, red y
  ventiladores: unos 4,4 kW con H100 NVL y 4,0 kW con L40S. El tablero y la refrigeración se
  dimensionan para ese máximo.
- **Fuentes a 220 V:** la red argentina cae en la franja de 200-220 V, donde cada fuente entrega
  hasta 2600 W; tres activas suman unos 7,8 kW con la cuarta de respaldo.
- **Calor:** unos 15.000 BTU/h por servidor. Con racks de 10 kW entran dos servidores por rack; con
  15 kW, tres. Temperatura de entrada recomendada entre 18 y 27 °C.
- **Red:** el tráfico es texto, así que 25 GbE alcanza. Tres redes separadas: servicio, datos y
  administración (BMC, nunca expuesta).
- **Sin salida a internet** para los servidores de modelos: los pesos se precargan desde un
  repositorio interno.

## Fuentes

- Ficha técnica: [Gigabyte G493-ZB3-AAP1 rev. 3.x](https://www.gigabyte.com/Enterprise/GPU-Server/G493-ZB3-AAP1-rev-3x)
  y [rev. 1.x](https://www.gigabyte.com/Enterprise/GPU-Server/G493-ZB3-AAP1-rev-1x).
- GPU validadas: [lista de compatibilidad de GPU, rev. 3.x](https://www.gigabyte.com/Enterprise/GPU-Server/G493-ZB3-AAP1-rev-3x/Support-QVL?CAT=PCIe-GPGPUCard).
- Arquitectura del modelo: [Qwen3-30B-A3B-Instruct-2507-FP8](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8).
- Mediciones de la plataforma: `docs/reports/resultados-pruebas.md`.
