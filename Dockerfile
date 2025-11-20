FROM python:3.14-slim

WORKDIR /app

# Install system deps for psycopg2
RUN apt-get update && apt-get install -y libpq-dev gcc curl && rm -rf /var/lib/apt/lists/*

COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

EXPOSE 5000

CMD ["python", "src/app.py"]
