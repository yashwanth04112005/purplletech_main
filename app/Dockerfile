FROM python:3.11-slim

WORKDIR /app

# System deps for OpenCV + psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc g++ libglib2.0-0 libsm6 libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY infra/ ./infra/
COPY pipeline/ ./pipeline/
COPY tests/ ./tests/
COPY pytest.ini .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
