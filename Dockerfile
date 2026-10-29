# The Fixpoint web app. Inside this container the container itself is the sandbox for test runs.
# Defaults to public demo mode (bundled examples only, daily run limits); override with -e FIXPOINT_PUBLIC_DEMO=0.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home app

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY fixpoint ./fixpoint
COPY examples ./examples
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    FIXPOINT_SANDBOX=local \
    FIXPOINT_EXAMPLES_DIR=/app/examples \
    FIXPOINT_PUBLIC_DEMO=1 \
    FIXPOINT_RUNS_PER_IP_PER_DAY=5 \
    FIXPOINT_MAX_RUNS_PER_DAY=100

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz')"
CMD ["sh", "-c", "exec uvicorn fixpoint.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
