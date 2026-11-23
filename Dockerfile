FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && pip install -r /app/requirements.txt

COPY . /app
RUN mkdir -p /app/data /app/staticfiles \
    && addgroup --system app \
    && adduser --system --ingroup app app \
    && chown -R app:app /app

USER app
EXPOSE 8000
CMD ["gunicorn", "iotlab.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60"]
