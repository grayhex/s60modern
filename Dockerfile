# syntax=docker/dockerfile:1.7
# Layers are ordered from the least to the most frequently changed one, so a code change rebuilds only the
# last layers: system packages → Python dependencies → native Symbian tools → frontend → application.

FROM node:26-trixie-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi \
    && npm ci --no-audit --no-fund
COPY frontend/ ./
COPY symport/web_assets/ /src/symport/web_assets/
COPY symport/phone/device_models.json /src/symport/phone/device_models.json
RUN npm run build

FROM python:3.14-slim-trixie AS runtime

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="SymPort" \
      org.opencontainers.image.description="Nokia S60/Symbian device workspace (local web UI)" \
      org.opencontainers.image.source="https://github.com/grayhex/s60modern" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends g++ libgammu-dev pkg-config ffmpeg gammu obexftp bluez openssh-client

WORKDIR /srv/e52modern

# Python dependencies come from pyproject.toml alone, so editing source files does not reinstall them.
COPY pyproject.toml ./
COPY tools/docker/requirements.py /tmp/requirements.py
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi \
    && python /tmp/requirements.py pyproject.toml > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt /tmp/requirements.py

# Keeps the existing Nokia Skin Compiler and bmconv toolchain (vendored sources, compiled once per change).
COPY tools/s60/ ./tools/s60/
RUN tools/s60/build.sh

COPY . .
COPY --from=frontend /src/symport/react_assets/ ./symport/react_assets/
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi \
    && pip install --no-deps -e . \
    && mkdir -p build workspace /symbian-auth \
    && chown 10001:10001 /symbian-auth \
    && chown -R 10001:10001 /srv/e52modern

USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -m symport.healthcheck
CMD ["python", "-m", "symport.web", "--host", "0.0.0.0", "--port", "8765"]
