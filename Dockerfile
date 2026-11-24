ARG DOCKER_REGISTRY=docker.io
FROM ${DOCKER_REGISTRY}/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/app \
    DJANGO_SETTINGS_MODULE=iotlab.settings

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 ca-certificates gosu ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && pip install -r /app/requirements.txt

COPY . /app
COPY docker/entrypoint.sh /usr/local/bin/iot-entrypoint
RUN chmod 0755 /usr/local/bin/iot-entrypoint \
    && mkdir -p /app/data/media /app/staticfiles /app/.streamlit \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app app \
    && chown -R app:app /app

USER root
ENTRYPOINT ["/usr/local/bin/iot-entrypoint"]
EXPOSE 8000 8501

CMD ["gunicorn", "iotlab.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "180", "--access-logfile", "-", "--error-logfile", "-"]
