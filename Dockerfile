FROM python:3.12-slim

WORKDIR /app

# Copy locally downloaded wheels and install offline — bypasses Depot builder
# PyPI connectivity issues entirely.
COPY requirements.txt .
COPY wheels ./wheels
RUN pip install --no-cache-dir --no-index --find-links=wheels -r requirements.txt && \
    rm -rf wheels

COPY . .

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
