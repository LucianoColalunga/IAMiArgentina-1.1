# IAMiArgentina en Kubernetes

Manifiestos de kustomize para correr la plataforma en Kubernetes. La `base` tiene los valores de
producción y los overlays cambian solo lo que difiere por entorno.

```
base/
  namespace.yaml      namespace iamia (Pod Security: baseline obligatorio, avisos de restricted)
  app/                roles de la app, config, secretos, KEDA, HPA, PDB, migrate e ingest
  postgres/           CloudNativePG: Cluster, Pooler (PgBouncer) y backups con Barman Cloud
  rabbitmq/           RabbitmqCluster, vhost, usuario y permisos
  redis/              Redis de caché (StatefulSet sin disco)
  models/             vLLM en GPU: llm y embeddings, caché de modelos y Job de descarga
  oauth2-proxy/       login OIDC de la consola
  ingress/            Ingress (clase nginx) y certificados de cert-manager
  networkpolicies/    default deny y permisos por componente
  monitoring/         ServiceMonitor, PodMonitor y PrometheusRule (kube-prometheus-stack)
overlays/
  production/         la base, más issuer e imagen de producción
  staging/            menos réplicas y recursos, APP_ENV=staging, hosts *.staging.example.org
```

## Roles

| Rol | Objetos | Escalado | Apagado |
|---|---|---|---|
| api | Deployment, Service, HPA | CPU al 60 %, de 3 a 12 | 30 s (preStop de 5 s) |
| worker | Deployment, ScaledObject | `iamia.inbound`, 40 mensajes listos por réplica, de 2 a 24 | 45 s |
| dispatcher-whatsapp | Deployment, ScaledObject | `iamia.outbound.whatsapp`, 200 por réplica, de 2 a 8 | 45 s |
| dispatcher-general | Deployment, ScaledObject | resto de `iamia.outbound.*`, 100 por réplica, de 2 a 6 | 45 s |
| maintenance | Deployment | 1 réplica, estrategia Recreate | 45 s |
| migrate | Job | hook de Argo CD o paso del pipeline | |
| ingest | CronJob | domingos 04:00 (hora argentina), `concurrencyPolicy: Forbid` | |

KEDA escala con `QueueLength` (mensajes listos en la cola) y, si no puede leer RabbitMQ, deja las
réplicas de `fallback`. WhatsApp tiene su propio dispatcher para que un pico de un canal no frene
a los demás.

## Requisitos del cluster

- Kubernetes 1.31 o posterior y un CNI que aplique NetworkPolicy (Calico, Cilium). Sin eso las
  políticas de `networkpolicies/` no hacen nada.
- Operadores y controladores:

| Componente | Para qué | Notas |
|---|---|---|
| CloudNativePG 1.26+ y el plugin Barman Cloud | PostgreSQL, PgBouncer y backups | el plugin necesita cert-manager |
| RabbitMQ Cluster Operator y Messaging Topology Operator | RabbitMQ, vhost, usuario y permisos | namespace `rabbitmq-system` |
| KEDA 2.x | escalado de worker y dispatchers | namespace `keda` |
| External Secrets Operator (API `external-secrets.io/v1`) | secretos desde Vault | `ClusterSecretStore` llamado `vault-backend` |
| cert-manager | certificados TLS | ClusterIssuer: ver placeholders |
| ingress-nginx | entrada HTTP | ver "Ingress" más abajo |
| NVIDIA GPU Operator | GPU para vLLM y sus métricas (`dcgm-exporter` con `dcgmExporter.serviceMonitor.enabled=true` y la etiqueta `release: kube-prometheus-stack`) | nodos con `nvidia.com/gpu.present=true` y taint `nvidia.com/gpu` |
| kube-prometheus-stack | métricas y alertas | release `kube-prometheus-stack`, namespace `monitoring` |

- StorageClass por defecto en SSD (PostgreSQL y RabbitMQ) y una StorageClass ReadWriteMany para
  la caché de modelos (`models/pvc.yaml`).
- Las NetworkPolicy asumen los namespaces `ingress-nginx`, `monitoring`, `keda`, `cnpg-system`,
  `rabbitmq-system` y `kube-system`. Si en el cluster se llaman distinto, ajustarlas.

## Secretos

No hay secretos en el repositorio. Cada `ExternalSecret` lee del `ClusterSecretStore`
`vault-backend`, y en cada cluster ese store apunta al mount de su entorno: los paths son los
mismos en staging y producción.

| Path en Vault | Claves | Secret que se genera | Lo usa |
|---|---|---|---|
| `iamia/app` | las de `base/app/secret.example.yaml` | `iamia-secrets` | roles de la app, migrate, ingest, KEDA (`AMQP_URL`), vLLM (`LLM_API_KEY`, `EMBEDDINGS_API_KEY`) |
| `iamia/postgres` | `password` | `iamia-db-app-credentials` | CloudNativePG (dueño de la base, usuario `iamia`) |
| `iamia/backup-s3` | `access_key_id`, `secret_access_key` | `iamia-db-backup-s3` | plugin Barman Cloud |
| `iamia/rabbitmq` | `username`, `password` | `iamia-rabbitmq-user` | Messaging Topology Operator |
| `iamia/redis` | `password` | `iamia-redis` | Redis (`requirepass`) |
| `iamia/oauth2-proxy` | `client_id`, `client_secret`, `cookie_secret` | `oauth2-proxy` | oauth2-proxy |

Hay valores que se repiten y nadie valida que coincidan: la contraseña de `iamia/postgres` va
también en `DATABASE_URL` y `DATABASE_URL_DIRECT`, la de `iamia/rabbitmq` en `AMQP_URL` y la de
`iamia/redis` en `REDIS_URL`. Si no coinciden, la readiness de la app falla.

- `DATABASE_URL` pasa por el pooler; `DATABASE_URL_DIRECT` va al primario y solo la usan migrate
  e ingest (ver `postgres/pooler.yaml`). La app no lee `DATABASE_URL_DIRECT`: los manifiestos se
  la pasan a esos dos procesos como `DATABASE_URL`.
- Las URLs llevan el FQDN del servicio (`*.iamia.svc`) porque KEDA las usa desde su namespace.
- Las claves propias de la app se generan con `python -m iamia gen-secrets`; el `cookie_secret`
  de oauth2-proxy, con `openssl rand -base64 32 | tr -- '+/' '-_'`.
- `secret.example.yaml` no está en `kustomization.yaml`: es la lista de claves. Para un cluster de
  prueba sin External Secrets se puede copiar, completar y aplicar a mano, nunca commitearlo
  completo.

## Despliegue

### Primera instalación

1. Operadores, `ClusterSecretStore`, ClusterIssuer y StorageClass (requisitos de arriba).
2. Cargar los secretos en Vault.
3. Dependencias, red y configuración (paso 1 del pipeline, más abajo).
4. Descargar los modelos al PVC (una vez, y otra cada vez que cambia un modelo):

   ```bash
   kubectl -n iamia apply -f infrastructure/kubernetes/base/models/model-download-job.yaml
   kubectl -n iamia wait --for=condition=complete job/vllm-model-download --timeout=2h
   ```

   Hasta que termine, vLLM reinicia (corre con `HF_HUB_OFFLINE=1`). Es esperado.
5. Migración y roles de la app (pasos 2 y 3 del pipeline).
6. Primera carga de la base de conocimiento, sin esperar al domingo:

   ```bash
   kubectl -n iamia create job --from=cronjob/ingest ingest-inicial
   ```

7. DNS de `asistente.example.org` y `consola.example.org` hacia el balanceador del ingress.
   cert-manager emite los certificados (HTTP-01; la política `acme-http01-solver` deja entrar al
   solver).

### Con Argo CD

Una Application por overlay (`path: infrastructure/kubernetes/overlays/production`). El orden
sale de las olas:

- Ola 0 (sin anotación): namespace, configuración, secretos, red, datos, modelos y consola.
- Ola 1: `migrate`, hook de `Sync` con `BeforeHookCreation`. No es `PreSync` porque en la primera
  instalación correría antes de que exista la base.
- Ola 2: api, worker, dispatchers, maintenance e ingest.

Argo CD pasa a la ola siguiente cuando la anterior está sana. Verificar que la versión instalada
tenga health checks para `Cluster` (CloudNativePG) y `RabbitmqCluster`; si no, agregarlos en
`resource.customizations.health`, o la migración puede arrancar antes que la base (el Job
reintenta hasta 4 veces). Como vLLM está en la ola 0, un cambio de imagen de vLLM demora el resto
del sync hasta que terminan de rotar sus réplicas.

### Sin Argo CD (pipeline)

kustomize no ordena recursos, así que el pipeline aplica en tres pasos con selectores de
etiquetas:

```bash
kubectl kustomize infrastructure/kubernetes/overlays/production > iamia.yaml

# 1. Todo menos los roles de la app y la migración.
kubectl apply --server-side -f iamia.yaml \
  -l 'app.kubernetes.io/component notin (api,worker,dispatcher,maintenance,ingest,migrate)'
kubectl -n iamia wait --for=condition=Ready cluster.postgresql.cnpg.io/iamia-db --timeout=20m
kubectl -n iamia wait --for=condition=AllReplicasReady rabbitmqcluster/iamia-rabbitmq --timeout=20m

# 2. Migración. El template de un Job no se puede modificar: se borra el anterior.
kubectl -n iamia delete job migrate --ignore-not-found
kubectl apply --server-side -f iamia.yaml -l app.kubernetes.io/component=migrate
kubectl -n iamia wait --for=condition=complete job/migrate --timeout=30m

# 3. Roles de la app.
kubectl apply --server-side -f iamia.yaml \
  -l 'app.kubernetes.io/component in (api,worker,dispatcher,maintenance,ingest)'
kubectl -n iamia rollout status deployment/api --timeout=10m
```

Las migraciones tienen que ser compatibles hacia atrás: los pods de la versión anterior siguen
atendiendo hasta el paso 3. Sin `--prune` los ConfigMap viejos (`iamia-config-<hash>`) quedan en el
cluster; borrarlos de vez en cuando.

### Renderizar y validar

```bash
kubectl kustomize infrastructure/kubernetes/overlays/production > prod.yaml
kubectl kustomize infrastructure/kubernetes/overlays/staging > staging.yaml

# Contra un cluster con los CRDs instalados:
kubectl apply --server-side --dry-run=server -f prod.yaml

# Sin cluster, con esquemas (los CRDs salen del catálogo de datree; los que no estén se saltean):
kubeconform -strict -summary -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  prod.yaml staging.yaml
```

## Capacidad

- La API corre un proceso por pod (`WEB_CONCURRENCY=1`): se escala con pods, no con procesos.
- GPU: la base trae 6 réplicas de `llm` y 1 de `embeddings`, lo que entra en un servidor de 8 GPU.
  Con N servidores, un patch en el overlay con `llm` = 6 x N y `embeddings` = N. El máximo de
  workers de KEDA tiene que acompañar: más workers que capacidad de vLLM solo arman cola adentro
  de vLLM.
- Conexiones a PostgreSQL: con todo al máximo (12 api + 24 worker + 14 dispatchers + maintenance)
  y `DB_POOL_MAX_SIZE=10`, la app puede pedir unas 510 conexiones. El pooler tiene 2 x (200 + 20)
  = 440 conexiones de servidor, dentro de `max_connections` (500); el resto espera en el pooler.
  Si se suben los máximos, revisar estas cuentas.

## Alta disponibilidad y recuperación

- PostgreSQL: 3 instancias en nodos distintos y replicación sincrónica a 1 réplica. Si cae el
  primario, CloudNativePG promueve una réplica sin perder commits. Backups: WAL continuo
  (`archive_timeout` de 5 minutos) y base diaria al object storage, con 30 días de retención.
- Restauración a un momento dado (PITR): un Cluster nuevo que arranca desde el backup.

  ```yaml
  apiVersion: postgresql.cnpg.io/v1
  kind: Cluster
  metadata:
    name: iamia-db-restore
  spec:
    instances: 3
    imageName: ghcr.io/cloudnative-pg/postgresql:16.10-standard-bookworm
    storage:
      size: 100Gi
    bootstrap:
      recovery:
        source: origen
        recoveryTarget:
          targetTime: "2026-01-01 12:00:00+00"
    externalClusters:
      - name: origen
        plugin:
          name: barman-cloud.cloudnative-pg.io
          parameters:
            barmanObjectName: iamia-db-backups
            serverName: iamia-db
  ```

  Si el cluster restaurado también archiva WAL, que use otro `serverName` para no mezclar sus WAL
  con los del original. Después se apunta el Pooler (`spec.cluster.name`) al cluster nuevo.
  Un backup que nunca se restauró no está probado: hacer un simulacro al menos cada trimestre.
- RabbitMQ: 3 nodos y colas quorum (las declara la app): sigue funcionando con 1 nodo caído. No
  necesita backup: los mensajes están guardados en PostgreSQL y el sweeper de maintenance
  republica cada 30 s los que no se procesaron. Ante una pérdida total, se recrea el cluster.
- Redis: caché reconstruible, una sola instancia sin persistencia (ver riesgos).
- vLLM: sin estado; los modelos están en el PVC o se vuelven a descargar con el Job.
- Distribución: topology spread por zona y por nodo en los roles con más de una réplica (vLLM
  solo por nodo, porque depende de dónde están las GPU), y anti-afinidad obligatoria por nodo más
  spread por zona en PostgreSQL y RabbitMQ (en PgBouncer la anti-afinidad es preferida).
- Objetivos ante la pérdida total del cluster de base de datos: RPO de 5 minutos o menos y RTO de
  30 minutos o menos. Son objetivos a validar con un simulacro, no garantías.

## Ingress

Se publican solo rutas explícitas (`/webhooks`, `/chat`, `/webchat`, `/v1/chat` y la raíz en
`asistente.example.org`; `/console`, `/admin/api` y `/oauth2` en `consola.example.org`, siempre a
través de oauth2-proxy). `/metrics` y las sondas no salen nunca. Límites: cuerpo de 3 MB (webhooks
de WhatsApp) y rate limit por IP en `/v1/chat` y `/webchat`. No se usan snippets.

Configuración recomendada del controlador (ConfigMap de ingress-nginx):

```yaml
allow-snippet-annotations: "false"
hsts: "true"
hsts-max-age: "31536000"
hsts-include-subdomains: "true"
limit-req-status-code: "429"
limit-conn-status-code: "429"
server-tokens: "false"
ssl-protocols: "TLSv1.2 TLSv1.3"
# IP real del cliente: PROXY protocol si el balanceador lo soporta...
use-proxy-protocol: "true"
# ...o confiar en X-Forwarded-For solo desde el rango del balanceador:
# use-forwarded-headers: "true"
# proxy-real-ip-cidr: "10.0.0.0/8"
```

La IP real importa: la app usa `X-Real-IP` (`TRUST_PROXY_HEADERS=true`) para sus límites y el
controlador lo completa con la IP que ve. Sin PROXY protocol ni `proxy-real-ip-cidr`, todos los
clientes comparten la IP del balanceador y el rate limit los corta a todos juntos.

ingress-nginx fue retirado (el mantenimiento terminó en marzo de 2026: no hay más versiones ni
parches de seguridad). Estos manifiestos lo usan porque es lo que hay instalado en muchos
clusters, pero conviene planificar la migración a Gateway API (HTTPRoute con una implementación
mantenida: Envoy Gateway, NGINX Gateway Fabric, Cilium, Istio). Los límites y HSTS pasan a las
políticas propias de cada implementación.

## Riesgos conocidos

- **La readiness de la API depende de Redis.** `/health/ready` chequea PostgreSQL, Redis y
  RabbitMQ. Con una sola instancia de Redis, un reinicio de Redis saca a todas las réplicas de la
  API del Service al mismo tiempo, aunque la app podría seguir sin Redis (fail open). Opciones:
  sacar Redis de la readiness en el backend o pasar a Redis con Sentinel o Valkey.
- **`EDGE_SHARED_SECRET`.** Si tiene valor, `/v1/chat` exige el header `X-Edge-Auth`. Sin snippets,
  ingress-nginx no puede agregarlo por ruta: hace falta un CDN o WAF adelante que lo agregue. Si
  no lo hay, dejar la clave vacía en `iamia/app` (la NetworkPolicy ya limita quién llega a la API);
  si no, `/v1/chat` responde 401.
- **Consola.** oauth2-proxy le pasa a la API el ID token como Bearer. La API exige
  `aud = OIDC_AUDIENCE` (`iamia-admin`) y lee los roles de `realm_access.roles`: el cliente de
  oauth2-proxy en el IdP tiene que ser `iamia-admin` y los roles tienen que viajar en el ID token
  (en Keycloak, "Add to ID token" en el mapper de roles).
- **vLLM con root de solo lectura.** Las cachés (`HOME`, `XDG_CACHE_HOME`, Numba, Triton,
  torch.compile) van a `/tmp` y se pierden al reiniciar: el arranque tarda más (la startupProbe
  da 30 minutos). Si una versión de vLLM escribe en otro lado, agregar un emptyDir; como último
  recurso, `readOnlyRootFilesystem: false` solo en ese contenedor.
- **Pod Security en baseline.** Los pods propios cumplen restricted, pero los que generan algunos
  operadores (RabbitMQ) pueden no cumplirlo según la versión. Si en el cluster cumplen, subir el
  enforce a restricted.
- **Staging asume un cluster aparte** (mismo namespace y su propio `vault-backend`). Si compartiera
  cluster con producción, cambiar el namespace y los FQDN de las URLs de los secretos.
- **Runbooks.** Las alertas enlazan a `docs/manual/runbooks.md#<ancla>` (lista de anclas en
  `../monitoring/README.md`).
- **vLLM fijado** en `v0.33.0` por digest (noviembre de 2026), sin probar en GPU: validarlo en el
  servidor antes de producción.
- **Placeholders a reemplazar:** StorageClass `change-me-rwx`,
  ClusterIssuer `change-me-cluster-issuer` (base) y `letsencrypt-production` (overlays), bucket
  `s3://CHANGE_ME/...` y endpoint `objectstore.example.org`, IdP `idp.example.org`, registro
  `registry.example.org`.
- **Imágenes a fijar por digest:** backend, `ghcr.io/cloudnative-pg/postgresql:16.10-standard-bookworm`,
  `rabbitmq:4.3-management`, `redis:8.6-alpine` y `quay.io/oauth2-proxy/oauth2-proxy:v7.12.0`.
