FROM nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_CACHE_DIR=/models \
    DOWNLOAD_TIMEOUT_SECONDS=600 \
    MAX_DOWNLOAD_BYTES=1073741824

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY deploy/runpod/requirements.txt ./requirements.txt
RUN python3 -m pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY deploy/runpod/handler.py ./handler.py

CMD ["python3", "handler.py"]
