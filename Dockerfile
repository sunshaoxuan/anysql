FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY anysql /app/anysql
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY docker /app/docker
COPY config.example.yaml /app/config.example.yaml

RUN chmod +x /app/docker/entrypoint.sh

EXPOSE 8765
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["api"]
