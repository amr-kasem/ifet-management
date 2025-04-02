FROM python:3.10

RUN pip install fastapi uvicorn

WORKDIR /app

COPY api_simulator.py /app/api_simulator.py

CMD ["uvicorn", "api_simulator:app", "--host", "0.0.0.0", "--port", "3197"]