# syntax=docker/dockerfile:1
#
# Multi-stage so the runtime image carries no build toolchain. Python 3.12 is
# pinned deliberately: 3.13/3.14 wheel coverage across this dependency chain
# (LangGraph, pandas, psycopg, pydantic-core) is not yet dependable.

# --------------------------------------------------------------------------- #
# Builder                                                                      #
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependency metadata first, so a source-only change does not invalidate the
# dependency layer — the slowest step by an order of magnitude.
COPY pyproject.toml README.md* ./
COPY app ./app
COPY evaluation ./evaluation
COPY scripts ./scripts

# Install into a virtualenv that is copied wholesale into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir .

# --------------------------------------------------------------------------- #
# Runtime                                                                      #
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root. A container that runs as root gives a container escape a head start,
# and nothing here needs privileged access.
RUN groupadd --system --gid 1001 datapilot \
 && useradd --system --uid 1001 --gid datapilot --create-home datapilot

WORKDIR /srv

COPY --from=builder /opt/venv /opt/venv
COPY --chown=datapilot:datapilot app ./app
COPY --chown=datapilot:datapilot evaluation ./evaluation
COPY --chown=datapilot:datapilot scripts ./scripts

USER datapilot

EXPOSE 8000

# Hits the real endpoint rather than merely checking the process is alive: a
# wedged application still has a running process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
