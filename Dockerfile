
# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

RUN apt-get update \
  && apt-get install -y --no-install-recommends tini curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY halloween_voting_app.py /app/

# App deps + WSGI server
RUN pip install --no-cache-dir \
  flask==3.0.0 \
  werkzeug==3.0.1 \
  gunicorn==21.2.0

# Data dirs + non-root runtime user
RUN mkdir -p /data /uploads \
  && addgroup --system app \
  && adduser --system --ingroup app app \
  && chown -R app:app /app /data /uploads

# Persist DB & uploads via volumes using symlinks from app cwd
# - DB expected at /app/halloween.db -> /data/halloween.db (symlink)
# - Uploads expected at /app/uploads    -> /uploads (symlink)
RUN ln -sf /data/halloween.db /app/halloween.db \
  && rm -rf /app/uploads \
  && ln -s /uploads /app/uploads

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fsS http://127.0.0.1:8000/ || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "-b", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "halloween_voting_app:app"]