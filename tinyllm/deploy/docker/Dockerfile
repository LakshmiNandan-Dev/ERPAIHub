# TinyLLM — NL → Oracle EBS SQL, packaged as a self-contained service entity for
# the ERP AI Hub (runs beside Ollama). Build context = repo root.
#
#   docker build -t tinyllm:latest -f deploy/docker/Dockerfile .
#   docker run -p 8000:8000 -v tinyllm_data:/data tinyllm:latest
#
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# CPU-only torch (the ~8M model needs no GPU) + serving stack + thin Oracle client
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.2" "sqlglot>=23" "fastapi>=0.110" "uvicorn>=0.29" \
        "pydantic>=2" "oracledb>=2"

# install the package (deps already present)
COPY pyproject.toml README.md LICENSE ./
COPY tinyllm ./tinyllm
RUN pip install --no-deps .

# bake the shipped vendor base (override by mounting your own /app/artifacts)
COPY artifacts/model_best.pt artifacts/tokenizer.json ./artifacts/

EXPOSE 8000
# extracted customer schemas + local fine-tunes persist here (mount a volume)
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["tinyllm", "serve", "--host", "0.0.0.0", "--port", "8000", "--data", "/data"]
