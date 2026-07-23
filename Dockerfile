FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY README.md ./

COPY . .

RUN pip install --upgrade pip
RUN pip install .

EXPOSE 8080

# Cloud Run injects PORT at runtime.  Keep 8080 as a local-container default.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
