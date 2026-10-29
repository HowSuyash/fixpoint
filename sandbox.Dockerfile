# Image that the agent's test runs execute in when FIXPOINT_SANDBOX is docker (or auto and this image exists).
# Runs are started with no network, 1 CPU, 1 GB RAM and a process limit (see fixpoint/sandbox.py).
#   docker build -f sandbox.Dockerfile -t fixpoint-sandbox .
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest
