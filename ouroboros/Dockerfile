# syntax=docker/dockerfile:1.7

FROM docker.io/library/python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b

ARG OUROBOROS_VERSION=v6.61.4
ARG OUROBOROS_SHA=a00d51dd414f794d830cacf7da760061e442fa88
ARG OUROBOROS_ARCHIVE_SHA256=b23c29ad47f0781c5414cd9d708bdcddfc9cf592188d1c09ff1d5265f1faa4b4

LABEL org.opencontainers.image.title="Communication Factory Ouroboros runtime" \
      org.opencontainers.image.source="https://github.com/razzant/ouroboros" \
      org.opencontainers.image.version="${OUROBOROS_VERSION}" \
      org.opencontainers.image.revision="${OUROBOROS_SHA}" \
      io.communication-factory.ouroboros.archive-sha256="${OUROBOROS_ARCHIVE_SHA256}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/ouroboros \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OUROBOROS_APP_ROOT=/var/lib/ouroboros \
    OUROBOROS_REPO_DIR=/runtime-repo \
    OUROBOROS_DATA_DIR=/home/ouroboros/Ouroboros/data \
    OUROBOROS_SETTINGS_PATH=/home/ouroboros/Ouroboros/data/settings.json \
    OUROBOROS_PID_FILE=/home/ouroboros/Ouroboros/data/state/ouroboros.pid \
    OUROBOROS_PORT_FILE=/home/ouroboros/Ouroboros/data/state/server_port \
    HOME=/home/ouroboros

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ouroboros \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/ouroboros ouroboros \
    && install -d -o 10001 -g 10001 \
        /data \
        /var/lib/ouroboros \
        /opt/communication-factory \
        /home/ouroboros/Ouroboros/data

COPY ouroboros/requirements.lock /tmp/ouroboros-requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/ouroboros-requirements.lock \
    && rm /tmp/ouroboros-requirements.lock

ADD --checksum=sha256:b23c29ad47f0781c5414cd9d708bdcddfc9cf592188d1c09ff1d5265f1faa4b4 \
    https://codeload.github.com/razzant/ouroboros/tar.gz/a00d51dd414f794d830cacf7da760061e442fa88 \
    /tmp/ouroboros.tar.gz
RUN install -d /opt/ouroboros \
    && tar -xzf /tmp/ouroboros.tar.gz --strip-components=1 -C /opt/ouroboros \
    && rm /tmp/ouroboros.tar.gz \
    && chown -R 10001:10001 /opt/ouroboros /opt/communication-factory

COPY --chown=10001:10001 ouroboros/runtime/ /opt/communication-factory/
COPY --chown=10001:10001 ouroboros/ouroboros.lock /opt/communication-factory/ouroboros.lock
COPY --chown=10001:10001 provider_profiles.py /opt/communication-factory/provider_profiles.py
COPY --chown=10001:10001 request_ledger.py /opt/communication-factory/request_ledger.py

WORKDIR /opt/ouroboros
EXPOSE 8765
ENTRYPOINT ["python", "/opt/communication-factory/entrypoint.py"]
CMD ["python", "/opt/communication-factory/runtime_launcher.py"]
