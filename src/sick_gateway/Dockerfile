FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sick_mqtt_gateway.py .

CMD ["python", "sick_mqtt_gateway.py"] 