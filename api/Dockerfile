FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ .

# TinyLLM NL->Oracle-SQL component (../tinyllm) — copied onto PYTHONPATH as
# plain files (no package install), matching how app/ itself already runs.
# All of its third-party deps are already covered by api/requirements.txt
# above except sqlglot, which is listed there explicitly for this reason.
COPY tinyllm/tinyllm ./tinyllm
COPY tinyllm/artifacts ./tinyllm/artifacts

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
