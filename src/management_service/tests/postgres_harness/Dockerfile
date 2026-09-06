FROM python:3.11-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
      sqlalchemy psycopg2-binary alembic pytest
CMD ["python", "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"]
