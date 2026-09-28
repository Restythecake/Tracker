# Tracker als Nest-Plugin. Ein Container, ein Port: Nest ruft /nest/v1/* über
# das Docker-Netz, der Browser sieht die Galerie über den Plugin-Host.
#
#   docker compose build                               # mit Browser (Stufe 2, siehe sources/fetch.py)
#   docker compose build --build-arg WITH_BROWSER=0    # schlank: nur HTTP, ~800 MB kleiner
FROM python:3.13-slim

ARG WITH_BROWSER=1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stufe 2: Scraplings StealthySession fährt Chromium über patchright. Statt
# "scrapling install" (lädt Chromium über playwright, inkl. headless-shell)
# holt patchright selbst genau die Revision, die es startet, samt System-
# bibliotheken (--with-deps, braucht root, darum hier im Build) und ohne die
# unbenutzte headless-shell. Der Browser liegt unter /opt, lesbar für alle,
# damit der unprivilegierte Nutzer ihn findet.
RUN if [ "$WITH_BROWSER" = "1" ]; then \
      pip install --no-cache-dir "scrapling[fetchers]==0.4.15" \
      && python -m patchright install --with-deps --no-shell chromium \
      && rm -rf /var/lib/apt/lists/* /root/.cache \
      && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"; \
    fi

COPY ebay_tracker/ ebay_tracker/
COPY nest_plugin/ nest_plugin/

# Unprivilegiert; /data ist das einzige, was geschrieben wird. Chromium will
# ein beschreibbares HOME (Profil-/Crash-Verzeichnisse), daher ein echtes.
RUN useradd --system --uid 10001 --create-home --home-dir /home/tracker tracker \
    && mkdir -p /data && chown tracker /data
USER tracker
ENV HOME=/home/tracker
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/nest/v1/health', timeout=2)" || exit 1

CMD ["python", "-m", "nest_plugin"]
