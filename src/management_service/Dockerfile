# syntax = docker/dockerfile:1.4

FROM tiangolo/uvicorn-gunicorn-fastapi:python3.9-slim

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY ./app ./app
COPY ./alembic.ini ./alembic.ini
COPY ./manage_db.py ./manage_db.py
COPY ./alembic ./alembic
COPY ./startup.sh ./startup.sh

# Make startup script executable
RUN chmod +x ./startup.sh

# Use our startup script instead of the default
CMD ["./startup.sh"]