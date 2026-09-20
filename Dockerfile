FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Application sources are baked in; third-party packages are installed by
# entrypoint.sh when the container starts.
COPY . /app

RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=25s --retries=3 \
    CMD python -c "import json,urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3); \
sys.exit(0 if json.load(r).get('status')=='ok' else 1)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
