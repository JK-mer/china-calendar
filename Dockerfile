# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PC_MCP_HOST=0.0.0.0 \
    PC_MCP_PORT=8804 \
    PC_STORE_DIR=/data/store \
    PC_SOURCES_FILE=/app/sources.yaml \
    PC_PROMPTS_DIR=/app/prompts

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY prompts ./prompts
COPY sources.yaml ./
COPY seeds ./seeds

RUN pip install --upgrade pip && pip install .

EXPOSE 8804

# Streamable-HTTP on 0.0.0.0 INSIDE the container; the compose port mapping
# (127.0.0.1 / 172.17.0.1 only) is the actual access control.
CMD ["pcal-mcp"]
