FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PET_DATA_DIR=/data HOME=/data/home PYTHONPATH=/srv
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY web ./web
COPY tests ./tests
# Non-root user; /data is the only writable location (a named volume).
RUN useradd -m pet && mkdir -p /data/home && chown -R pet /data
USER pet
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
