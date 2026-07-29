# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS dependencies

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p src/dispute_agent \
    && touch src/dispute_agent/__init__.py \
    && python -m pip wheel --wheel-dir /dependency-wheels . \
    && find /dependency-wheels -name 'xianyu_dispute_agent-*.whl' -delete

FROM python:3.12-slim-bookworm AS application

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --no-deps --wheel-dir /application-wheel .

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    XIANYU_PROJECT_ROOT=/app

RUN groupadd --gid 10001 xianyu \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin xianyu
COPY --from=dependencies /dependency-wheels /dependency-wheels
COPY --from=application /application-wheel /application-wheel
RUN python -m pip install /dependency-wheels/* /application-wheel/* \
    && rm -rf /dependency-wheels /application-wheel

WORKDIR /app
COPY --chown=xianyu:xianyu alembic ./alembic
COPY --chown=xianyu:xianyu alembic.ini ./alembic.ini
COPY --chown=xianyu:xianyu config ./config
COPY --chown=xianyu:xianyu evaluation ./evaluation
COPY --chown=xianyu:xianyu policies ./policies
COPY --chown=xianyu:xianyu schemas ./schemas
COPY --chown=xianyu:xianyu scripts ./scripts
COPY --chown=xianyu:xianyu web ./web

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "dispute_agent.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
