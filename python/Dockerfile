FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY python/main.py .

CMD ["sh", "-c", "gunicorn --workers 1 --threads 4 --bind 0.0.0.0:${PORT:-8080} main:app"]
