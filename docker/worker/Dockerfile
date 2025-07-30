FROM python:3.11-slim

WORKDIR /app
COPY worker/worker.py worker/requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "worker.py"]