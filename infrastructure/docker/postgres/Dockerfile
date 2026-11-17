# syntax=docker/dockerfile:1
# PostgreSQL 16 con pgvector compilado desde el código fuente sobre la imagen oficial alpine.
# Se compila sin optimizaciones específicas de la CPU (OPTFLAGS="") para que la imagen sirva en
# cualquier servidor x86-64 y sin bitcode JIT (with_llvm=no) para no instalar clang/llvm.
ARG POSTGRES_IMAGE=postgres:16-alpine
ARG PGVECTOR_VERSION=0.8.7

FROM ${POSTGRES_IMAGE} AS build
ARG PGVECTOR_VERSION
RUN apk add --no-cache --virtual .build build-base curl \
    && curl -fsSL "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" -o /tmp/pgvector.tar.gz \
    && mkdir -p /tmp/pgvector && tar -xzf /tmp/pgvector.tar.gz -C /tmp/pgvector --strip-components=1 \
    && cd /tmp/pgvector \
    && make OPTFLAGS="" with_llvm=no \
    && make install with_llvm=no DESTDIR=/out

FROM ${POSTGRES_IMAGE}
COPY --from=build /out/usr/local/lib/postgresql/ /usr/local/lib/postgresql/
COPY --from=build /out/usr/local/share/postgresql/extension/ /usr/local/share/postgresql/extension/
COPY initdb/ /docker-entrypoint-initdb.d/
# Arranca directamente como el usuario postgres (la imagen oficial arranca como root y baja de
# privilegios en el entrypoint). El directorio de datos de la imagen ya es de postgres, así que un
# volumen nuevo se crea con ese dueño; un tmpfs o un directorio del host tiene que pertenecer al
# uid 70. Sin root, el entrypoint no usa gosu: se quita (es un binario de Go que los escáneres
# reportan por la versión de su biblioteca estándar).
RUN rm -f /usr/local/bin/gosu
USER postgres
