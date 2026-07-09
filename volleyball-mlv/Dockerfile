FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY parser.py fetch.py bigquery_loader.py main.py ./

ENTRYPOINT ["python", "main.py"]
