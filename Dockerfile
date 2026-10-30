# ============================================================
# Research pipeline container
# Base: official R image (pin the version you want reproducible)
# ============================================================
FROM rocker/r-ver:4.4.1

# --- system libraries needed by the R package stack ----------
# curl/httr2 -> libcurl; xml2 (europepmc) -> libxml2; openssl -> libssl
# cron for in-container scheduling; tzdata so timestamps are sane
RUN apt-get update && apt-get install -y --no-install-recommends \
      libcurl4-openssl-dev \
      libssl-dev \
      libxml2-dev \
      cron \
      tzdata \
      ca-certificates \
      python3 \
      python3-cryptography \
    && rm -rf /var/lib/apt/lists/*

# --- timezone (override at build: --build-arg TZ=Europe/Berlin) ---
ARG TZ=UTC
ENV TZ=${TZ}
RUN ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && echo ${TZ} > /etc/timezone

# --- R packages baked into the image -------------------------
# No per-package version pins: reproducibility here rests entirely on the
# pinned rocker base tag above, whose built-in repo is a dated CRAN snapshot.
# That is a deliberate trade-off -- change the base tag and the package set
# moves with it, so treat a base-image bump as a dependency upgrade.
RUN install2.r --error --skipinstalled \
      yaml \
      rentrez \
      europepmc \
      httr2 \
      jsonlite \
      DBI \
      filelock \
      RSQLite \
      stringdist \
      blastula \
      htmltools \
      ctrdata \
      nodbi \
      rvest \
      xml2 \
    && rm -rf /tmp/downloaded_packages

WORKDIR /app

# Copy the pipeline code (configs + db + digests come from volumes)
COPY R/ ./R/
COPY web/decrypt_key.py ./web/decrypt_key.py
COPY scripts/write_cron_env.py ./scripts/write_cron_env.py
COPY config/ ./config/
COPY entrypoint.sh ./entrypoint.sh
COPY crontab.template ./crontab.template

RUN chmod +x ./entrypoint.sh ./R/run_all.R

# Persistent state + outputs live on mounted volumes
VOLUME ["/app/db", "/app/digests", "/app/config"]

ENTRYPOINT ["./entrypoint.sh"]
