FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_CACHE_DIR=/models \
    ALIGN_MODEL_CACHE_DIR=/models/alignment \
    WHISPERX_BATCH_SIZE=16 \
    DOWNLOAD_CONNECT_TIMEOUT_SECONDS=15 \
    DOWNLOAD_READ_TIMEOUT_SECONDS=60 \
    DOWNLOAD_MAX_ATTEMPTS=3 \
    MAX_DOWNLOAD_BYTES=1073741824

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY deploy/runpod/requirements.txt ./requirements.txt
RUN python3 -m pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY deploy/runpod/handler.py ./handler.py

CMD ["python3", "handler.py"]
