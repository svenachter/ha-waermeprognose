# The Supervisor passes BUILD_FROM=ghcr.io/home-assistant/base (Alpine without Python).
# This app needs Python, so the base image is pinned here on purpose and BUILD_FROM is ignored.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/waermeprognose
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8099
CMD ["python", "-m", "app"]
