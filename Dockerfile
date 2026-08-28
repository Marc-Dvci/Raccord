# Raccord application image.
#
# Two stages so the runtime layer carries no build toolchain, and a non-root
# user because the container that holds the approval signing key should have as
# little as possible (docs/THREAT_MODEL.md).
#
#   docker build -t raccord .
#   docker run --rm -p 8080:8080 raccord
#
# The image runs the full demonstration with no credentials: reasoning defaults
# to offline and the MCP transport defaults to the in-process server, so the
# container is judge-runnable as-is (docs/JUDGE.md).

FROM python:3.12-slim AS build

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Package the judge-facing benchmark proof inside the installed wheel. Reading
# a repository-relative path works in editable installs but not in /opt/venv.
COPY bench/results/summary.json ./src/raccord/data/benchmark_summary.json

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[cloud,otel]"

# ---------------------------------------------------------------------------

FROM python:3.12-slim

LABEL org.opencontainers.image.title="Raccord" \
      org.opencontainers.image.description="Accessible Experience Reliability for live media" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/Marc-Dvci/Raccord"

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RACCORD_API_PORT=8080 \
    RACCORD_DATA_DIR=/var/raccord

# The observability configuration and the generated Grafana assets travel with
# the image so a deployment provisions the same dashboards and alert rules the
# SLO definitions produced (tools/generate_grafana_assets.py).
WORKDIR /app
COPY observability ./observability
COPY docs ./docs

RUN useradd --system --uid 10001 --home /var/raccord raccord \
 && mkdir -p /var/raccord \
 && chown -R raccord:raccord /var/raccord /app
USER raccord

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2).status == 200 else 1)"

CMD ["raccord", "serve", "--host", "0.0.0.0", "--port", "8080"]
