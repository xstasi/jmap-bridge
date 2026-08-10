FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY config ./config

RUN pip install --no-cache-dir .

ENV JMAP_BRIDGE_CONFIG=/app/config/domains.test.yaml
ENV JMAP_BRIDGE_BASE_URL=http://localhost:8080
EXPOSE 8080

CMD ["python", "-m", "jmap_bridge.app"]
