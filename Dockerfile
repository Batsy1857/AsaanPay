FROM python:3.13-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# CI explicitly selects release. Only runtime files enter the release image.
FROM runtime AS release
# Patch the base's zlib within Debian 13; runtime does not need pip or its vendors.
# Keep build/development tooling available in the separate development target.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip
ARG APP_VERSION
ARG GIT_COMMIT
ARG RUN_ID
ARG RUN_ATTEMPT
ARG RELEASE_ID
RUN python -m app.version --write "$APP_VERSION" "$GIT_COMMIT" "$RUN_ID" "$RUN_ATTEMPT" \
    && python -c 'import os; from app.version import read_metadata; assert read_metadata(required=True)["release_id"] == os.environ["RELEASE_ID"]'
LABEL org.opencontainers.image.title="AsaanPay" \
      org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.revision=$GIT_COMMIT \
      io.asaanpay.release-id=$RELEASE_ID \
      io.asaanpay.run-id=$RUN_ID \
      io.asaanpay.run-attempt=$RUN_ATTEMPT
USER 10001:10001
CMD ["sh", "-ec", "python -m app.version >/dev/null; exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]

# Last target preserves `docker compose up --build` and in-container test commands.
FROM runtime AS development
COPY tests ./tests
COPY scripts ./scripts
COPY VERSION ./VERSION
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
