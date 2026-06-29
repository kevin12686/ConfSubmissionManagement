FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/venv/bin:${PATH}"

WORKDIR /app

RUN python -m venv /venv

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

COPY docker/entrypoint.sh /usr/local/bin/sms-entrypoint
RUN chmod +x /usr/local/bin/sms-entrypoint

COPY . /app

EXPOSE 8000

ENTRYPOINT ["sms-entrypoint"]
