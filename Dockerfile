FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY README.md ./

COPY . .

RUN pip install --upgrade pip
RUN pip install .

EXPOSE 8080

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]