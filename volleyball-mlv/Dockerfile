FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY parser.py fetch.py _extract_pdf_text.py bigquery_loader.py main.py ./

ENTRYPOINT ["python", "main.py"]
