FROM python:3.12-slim
WORKDIR /app
COPY platform/ platform/
COPY ops/ ops/
COPY registry/ registry/
RUN pip install --no-cache-dir ./platform
ENV RAW_STORE_DIR=/data/raw
# Worker (default). API service overrides CMD when it exists (Phase B wave 2).
CMD ["gm", "worker", "--with-scheduler"]
