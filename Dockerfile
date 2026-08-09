# HPA 細胞株 遺伝子発現エクスプローラー
#
# The image contains the application only.  The database is large (~900 MiB for
# a full HPA release) and is built from your own copies of the HPA files, so it
# lives in a volume at /data rather than in the image - which also means
# updating the data never requires a rebuild.
#
#   docker compose run --rm build --expression /source/rna_celline.tsv.zip
#   docker compose up -d

FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="HPA cell line expression explorer" \
      org.opencontainers.image.description="Gene expression across human cell lines, from Human Protein Atlas data" \
      org.opencontainers.image.source="https://github.com/pepsea/cell_line_expression_analysis" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HPA_CELLEXP_DB=/data/hpa_cellexp.sqlite \
    HPA_CELLEXP_HOST=0.0.0.0 \
    HPA_CELLEXP_PORT=8000 \
    HPA_CELLEXP_WORKERS=1

WORKDIR /app

# Dependencies first so application edits do not invalidate the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY hpa_cellexp/ ./hpa_cellexp/
COPY README.md ./

# Unprivileged, with a home the app never needs to write to.  /data is created
# here so a named volume inherits the right ownership on first use.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
    && mkdir -p /data \
    && chown -R app:app /data /app
USER app

EXPOSE 8000
VOLUME ["/data"]

# Uses /api/meta rather than /api/health: health only reports that the file
# exists, while meta actually opens the database, so a stale or unreadable one
# is reported as unhealthy instead of quietly failing every query later.
# Reads HPA_CELLEXP_PORT so it follows the app when the port is overridden.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:' + os.environ.get('HPA_CELLEXP_PORT', '8000') + '/api/meta', timeout=4).status == 200 else 1)"

# Host and port come from HPA_CELLEXP_HOST / HPA_CELLEXP_PORT above rather than
# from the command line, so `docker run -e HPA_CELLEXP_PORT=9000` is enough to
# move the listener - no need to override CMD, and the healthcheck follows.
ENTRYPOINT ["python", "-m", "hpa_cellexp"]
CMD ["serve"]
