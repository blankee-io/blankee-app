# Blankee application image.
#
# Serves with gunicorn rather than Apache/mod_wsgi: in a container there is no
# reason for a process manager inside the image, and gunicorn is one process to
# supervise instead of two.
#
# Python 3.10 to match what the application is developed and run on.
FROM python:3.10-slim

# libmariadb / build tools are here for the same reason they are usually absent:
# cryptography 3.4.8 is old enough that a wheel may not exist for every
# platform, and without a compiler the build fails late and confusingly. The
# mysql client is needed at runtime, not build time - install/migrate.py drives
# it to apply schema.sql, which contains DELIMITER directives that only the
# client understands.
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-mysql-client \
        build-essential \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn==23.0.0

COPY . .

# Profile pictures are written here, and the config file the app maintains lives
# in /config. Both are volumes in compose; creating them here means the image
# also works without one.
RUN mkdir -p /app/static/uploads /config \
    && chmod +x /app/install/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    BLANKEE_CONFIG=/config/blankee.conf \
    DB_HOST=db \
    DB_NAME=blankee \
    DB_USER=blankee \
    REDIS_HOST=redis \
    REDIS_PORT=6379 \
    BANK_PROVIDER=null \
    ENRICHMENT_PROVIDER=null

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/redis || exit 1

ENTRYPOINT ["/app/install/docker-entrypoint.sh"]

# Two workers, threaded. The application keeps a SQLAlchemy connection pool per
# process, so worker count multiplies database connections - keep it modest
# unless MySQL's max_connections has been raised to match.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", \
     "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
