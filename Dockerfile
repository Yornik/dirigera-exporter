# syntax=docker/dockerfile:1

# ---- build stage: install pinned deps into an isolated venv ----
FROM python:3.14-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements.txt

# ---- runtime stage: slim, non-root, no build tooling ----
FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY dirigera_exporter.py .

EXPOSE 9119
# nonroot uid (matches the cluster's runAsNonRoot/runAsUser 65532)
USER 65532:65532
ENTRYPOINT ["python", "-u", "dirigera_exporter.py"]
