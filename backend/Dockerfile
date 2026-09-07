# SupportMesh customer-service system -- multi-stage Docker build
# Goal: keep the production image lean; the development image carries debug tooling

# ── Stage 1: base environment ─────────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# curl is for the health check; gcc/g++ are no longer needed (the local ML model is gone)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: install Python dependencies ──────────────────────────────────────
FROM base AS dependencies

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Pre-download ChromaDB's built-in ONNX embedding model (~79MB) so the first request does not time out
RUN mkdir -p /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2 && \
    curl -L --retry 3 --retry-delay 5 -o /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx.tar.gz \
    https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz && \
    cd /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2 && \
    tar -xzf onnx.tar.gz && \
    rm onnx.tar.gz

# ── Stage 3: production image ─────────────────────────────────────────────────
FROM base AS production

# Run as a non-root user. Create the user first so later COPYs carry the owner, avoiding a large extra layer from chown -R.
RUN useradd -m -u 1000 supportmesh

# Copy the installed packages from the dependencies stage
COPY --from=dependencies /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=dependencies /usr/local/bin /usr/local/bin
# Copy the pre-downloaded ONNX model cache
COPY --from=dependencies --chown=supportmesh:supportmesh /root/.cache/chroma /home/supportmesh/.cache/chroma

# Copy the application code
COPY --chown=supportmesh:supportmesh . .

# Create the directories the app needs, adjusting permissions only where it writes at runtime, rather than chowning the whole app.
RUN mkdir -p /app/data/chroma /app/logs /app/config && \
    chown supportmesh:supportmesh /app/data /app/data/chroma /app/logs /app/config
USER supportmesh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ── Stage 4: development image ────────────────────────────────────────────────
FROM dependencies AS development

COPY . .

RUN mkdir -p /app/data/chroma /app/logs /app/config /app/tests && \
    chmod -R 777 /app/data /app/logs

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
