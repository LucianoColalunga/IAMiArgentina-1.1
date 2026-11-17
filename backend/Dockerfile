# syntax=docker/dockerfile:1
# Imagen única para todos los roles (api, worker, dispatcher, maintenance, migrate, ingest).
# Se construye desde la raíz del repositorio:
#   docker build -f backend/Dockerfile --target runtime -t iamia/backend:1.1.0 .
#   docker build -f backend/Dockerfile --target test -t iamia/backend:test .
ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken \
    IAMIA_FRONTEND_DIR=/app/frontend \
    PYTHONPATH=/app/backend

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt \
    # El vocabulario del contador de tokens queda dentro de la imagen: sin descargas en
    # tiempo de ejecución (los servidores on-premise pueden no tener salida a internet).
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

# Parches de seguridad de Debian publicados después de la imagen base.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY backend/iamia backend/iamia
COPY frontend frontend
COPY data data

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin iamia

# ------------------------------------------------------------------ pruebas
FROM base AS test
COPY backend/requirements-dev.txt backend/requirements-dev.txt
RUN pip install -r backend/requirements-dev.txt
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/tests backend/tests
WORKDIR /app/backend
USER 10001
ENTRYPOINT []
CMD ["python", "-m", "pytest"]

# ------------------------------------------------------------------ producción (última: es la de por defecto)
FROM base AS runtime
# pip no se usa al correr y trae sus propias copias de urllib3, msgpack y setuptools, que los
# escáneres reportan aparte de las dependencias de la aplicación.
RUN pip uninstall -y pip
USER 10001
EXPOSE 8000 9100
ENTRYPOINT ["python", "-m", "iamia"]
CMD ["api"]
