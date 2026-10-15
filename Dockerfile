# The ZacoAgents API and frontend in one container, for Azure Container Apps.
# Same code Vercel runs through api/index.py, served directly by uvicorn.

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/app ./backend/app
COPY backend/data ./backend/data
COPY frontend ./frontend

RUN useradd --system --uid 10001 zacon && chown -R zacon /app
USER zacon

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# Azure's ingress sits in front and terminates TLS; trust its forwarded headers.
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "2"]
