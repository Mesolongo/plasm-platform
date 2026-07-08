# plsem-platform — single image with the R engine (seminr) + Python backend.
#
#   docker build -t plsem-platform .
#   docker run -p 8000:8000 -v plsem_data:/app/data \
#     -e ANTHROPIC_API_KEY=sk-ant-... plsem-platform
#
# ANTHROPIC_API_KEY is optional: without it the app runs fully, minus the AI
# features (model architect, interpretation, chat, manuscript drafting).
# PDF export needs LibreOffice; build with --build-arg LIBREOFFICE=1 (~+500 MB).
FROM rocker/r-ver:4.6.1

# R engine packages (rocker preconfigures a binary CRAN mirror)
RUN Rscript -e 'install.packages(c("seminr", "jsonlite")); \
                stopifnot(all(c("seminr", "jsonlite") %in% rownames(installed.packages())))'

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

ARG LIBREOFFICE=0
RUN if [ "$LIBREOFFICE" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends libreoffice-writer \
      && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/opt/venv/bin:$PATH"

COPY engine/R/ engine/R/
COPY ai/ ai/
COPY backend/app/ backend/app/
COPY frontend/ frontend/

VOLUME /app/data
EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
