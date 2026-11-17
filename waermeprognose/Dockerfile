# The Supervisor passes BUILD_FROM=ghcr.io/home-assistant/base (Alpine without Python).
# This app needs Python, so the base image is pinned here on purpose and BUILD_FROM is ignored.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

WORKDIR /opt/waermeprognose
COPY requirements.txt .
# Fully pinned + binary wheels only; retry the whole install on flaky connections.
RUN for i in 1 2 3; do \
      python -m pip install --only-binary=:all: -r requirements.txt && exit 0; \
      echo "pip install fehlgeschlagen (Versuch $i/3), neuer Versuch in 20 s"; sleep 20; \
    done; exit 1

COPY app ./app

EXPOSE 8099
CMD ["python", "-m", "app"]
