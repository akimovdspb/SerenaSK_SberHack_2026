# syntax=docker/dockerfile:1.7

FROM docker.io/library/node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS web-build
WORKDIR /src
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
RUN npm ci --ignore-scripts
COPY apps/web/ apps/web/
RUN npm run build --workspace @communication-factory/web

FROM docker.io/library/caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d AS caddy-bin

FROM docker.io/library/python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b

ARG OUROBOROS_VERSION=v6.61.4
ARG OUROBOROS_SHA=a00d51dd414f794d830cacf7da760061e442fa88
ARG OUROBOROS_ARCHIVE_SHA256=b23c29ad47f0781c5414cd9d708bdcddfc9cf592188d1c09ff1d5265f1faa4b4

LABEL org.opencontainers.image.title="Communication Factory Railway demo" \
      org.opencontainers.image.source="https://github.com/fruitpicker01/SberAIHackDev" \
      org.opencontainers.image.version="${OUROBOROS_VERSION}" \
      org.opencontainers.image.revision="${OUROBOROS_SHA}" \
      io.communication-factory.deployment-profile="railway" \
      io.communication-factory.ouroboros.archive-sha256="${OUROBOROS_ARCHIVE_SHA256}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CF_DEPLOYMENT_PROFILE=railway \
    CF_RUNTIME_PROVIDER=openrouter \
    CF_PROVIDER_PROFILE=openrouter-glm-5.2-campaign-authoring \
    OPENROUTER_ENABLED=true \
    OUROBOROS_MODEL=openrouter::z-ai/glm-5.2 \
    OUROBOROS_VERSION=${OUROBOROS_VERSION} \
    OUROBOROS_SHA=${OUROBOROS_SHA} \
    TOTAL_BUDGET=20 \
    OUROBOROS_PER_TASK_COST_USD=2 \
    AUTO_BOOTSTRAP_SKILL_REVIEW=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10004 contract \
    && groupadd --gid 10001 ouroboros \
    && groupadd --gid 10002 factory \
    && groupadd --gid 10003 gateway \
    && useradd --uid 10001 --gid 10001 --groups 10004 --create-home --home-dir /home/ouroboros ouroboros \
    && useradd --uid 10002 --gid 10002 --groups 10004 --create-home --home-dir /home/factory factory \
    && useradd --uid 10003 --gid 10003 --create-home --home-dir /home/gateway gateway \
    && install -d /opt/communication-factory /opt/ouroboros /srv /skills /projection \
    && install -d -o 10001 -g 10001 /home/ouroboros/Ouroboros/data /var/lib/ouroboros \
    && install -d -o 10002 -g 10002 /srv/app /var/lib/communication-factory/app \
    && install -d -o 10003 -g 10003 /home/gateway/.config /home/gateway/.local/share \
    && install -d -m 2770 -o 10001 -g 10004 /var/lib/communication-factory/contracts

COPY ouroboros/requirements.lock /tmp/ouroboros-requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/ouroboros-requirements.lock \
    && rm /tmp/ouroboros-requirements.lock

COPY apps/requirements.lock /tmp/app-requirements.lock
RUN python -m venv /opt/app-venv \
    && /opt/app-venv/bin/python -m pip install --no-cache-dir --require-hashes -r /tmp/app-requirements.lock \
    && rm /tmp/app-requirements.lock

ADD --checksum=sha256:b23c29ad47f0781c5414cd9d708bdcddfc9cf592188d1c09ff1d5265f1faa4b4 \
    https://codeload.github.com/razzant/ouroboros/tar.gz/a00d51dd414f794d830cacf7da760061e442fa88 \
    /tmp/ouroboros.tar.gz
RUN tar -xzf /tmp/ouroboros.tar.gz --strip-components=1 -C /opt/ouroboros \
    && rm /tmp/ouroboros.tar.gz \
    && chown -R 10001:10001 /opt/ouroboros

COPY --chown=10001:10001 ouroboros/runtime/ /opt/communication-factory/
COPY --chown=10001:10001 ouroboros/ouroboros.lock /opt/communication-factory/ouroboros.lock
COPY --chown=10001:10001 provider_profiles.py /opt/communication-factory/provider_profiles.py
COPY --chown=10001:10001 request_ledger.py /opt/communication-factory/request_ledger.py
COPY --chown=10001:10001 ouroboros/skills/ /skills/
COPY --chown=10001:10001 prompts/communication_factory.ru.md /projection/communication_factory.ru.md

COPY --chown=10002:10002 apps/__init__.py /srv/app/apps/__init__.py
COPY --chown=10002:10002 apps/api/ /srv/app/apps/api/
COPY --chown=10002:10002 data/synthetic/ /srv/app/data/synthetic/
COPY --chown=10002:10002 data/editorial/ /srv/app/data/editorial/
COPY --chown=10002:10002 reports/basket03-mvp-testing/ /srv/app/reports/basket03-mvp-testing/
COPY --chown=10002:10002 provider_profiles.py /srv/app/provider_profiles.py
COPY --chown=10002:10002 request_ledger.py /srv/app/request_ledger.py
COPY --chown=10003:10003 --from=web-build /src/apps/web/dist /srv
COPY --from=caddy-bin /usr/bin/caddy /usr/bin/caddy
COPY railway/Caddyfile /etc/caddy/Caddyfile
COPY railway/__init__.py /opt/communication-factory/railway/__init__.py
COPY railway/auth.py /opt/communication-factory/railway/auth.py
COPY railway/start.py /opt/communication-factory/railway_start.py
COPY railway/check_admission.py /opt/communication-factory/check_admission.py

WORKDIR /opt/communication-factory
EXPOSE 8080
ENTRYPOINT ["python", "/opt/communication-factory/railway_start.py"]
