# Tracker als Nest-Plugin. Ein Container, ein Port: Nest ruft /nest/v1/* über
# das Docker-Netz, der Browser sieht die Galerie über den Plugin-Host.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ebay_tracker/ ebay_tracker/
COPY nest_plugin/ nest_plugin/

# Unprivilegiert; /data ist das einzige, was geschrieben wird.
RUN useradd --system --uid 10001 --home /app tracker && mkdir -p /data && chown tracker /data
USER tracker
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/nest/v1/health', timeout=2)" || exit 1

CMD ["python", "-m", "nest_plugin"]
