# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /bin/uv
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv venv /opt/venv && VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="api-qovrix" \
      org.opencontainers.image.description="Contract-aware API monitoring: catches breaking changes, not just downtime." \
      org.opencontainers.image.source="https://github.com/jsxd3v/api-qovrix" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 qovrix && mkdir /work && chown qovrix /work
USER qovrix
WORKDIR /work
ENTRYPOINT ["api-qovrix"]
CMD ["run"]
