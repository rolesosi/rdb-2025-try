FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONHASHSEED=random \
    PYTHONIOENCODING=UTF-8

WORKDIR /app
COPY worker/worker.py worker/requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV BATCH_SIZE=10 \
    MAX_RETRIES=3 \
    HTTP_TIMEOUT=3 \
    POLL_TIMEOUT=5    

CMD ["python", "-O","worker.py"]
                