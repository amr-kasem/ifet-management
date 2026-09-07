FROM python:3.11-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*
# fastapi/httpx were added 2026-09-08 so the manual-test routes can be exercised
# here rather than only unit-tested. The routes are where the phase rules live -
# a verdict before termination, a second verdict, evidence after review - and
# none of those is observable from the models alone.
RUN pip install --no-cache-dir \
      sqlalchemy psycopg2-binary alembic pytest \
      fastapi pydantic 'httpx<0.28' python-multipart reportlab
CMD ["python", "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"]
