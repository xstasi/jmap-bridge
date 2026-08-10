# Production image for the JMAP bridge. Distinct from docker/Dockerfile,
# which builds a test-only image for tests/fixtures/docker-compose.test.yml
# (bakes in domains.test.yaml and points at fake in-network Dovecot/
# Radicale containers - not meant to run against real backends).
#
# This image ships no domains.yaml at all - the bridge is a pure,
# zero-local-storage protocol translator, so the only thing a deployment
# needs to supply at runtime is your own config/domains.yaml (mount it as
# a volume) and, if you want this container to terminate TLS directly, a
# certificate pair (see compose.yml for both options).

FROM python:3.12-slim

# Runs as a non-root user - unlike docker/Dockerfile's test image (an
# ephemeral, disposable container where this doesn't matter), a
# production deployment should never run network-facing code as root.
RUN useradd --create-home --uid 1000 jmapbridge

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY docker/healthcheck.py ./healthcheck.py

RUN pip install --no-cache-dir . \
    && rm -rf /root/.cache/pip

USER jmapbridge

# No JMAP_BRIDGE_CONFIG/JMAP_BRIDGE_BASE_URL default here (unlike
# docker/Dockerfile's test image) - both are deployment-specific and must
# be set explicitly (see compose.yml), so a misconfigured deployment
# fails loudly at startup rather than silently running with test-fixture
# or localhost-only values.
EXPOSE 8080

# See docker/healthcheck.py for why a plain 2xx-or-bust check is wrong here
# (every real route requires auth or redirects) and what counts as healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python healthcheck.py

CMD ["python", "-m", "jmap_bridge.app"]
