# syntax=docker/dockerfile:1.7

FROM docker.io/library/python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/srv/app

RUN groupadd --gid 10002 factory \
    && useradd --uid 10002 --gid 10002 --create-home factory \
    && install -d -o 10002 -g 10002 /srv/app /data /contract-lock

COPY apps/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

COPY --chown=10002:10002 apps/__init__.py /srv/app/apps/__init__.py
COPY --chown=10002:10002 apps/api/ /srv/app/apps/api/
COPY --chown=10002:10002 data/synthetic/ /srv/app/data/synthetic/
COPY --chown=10002:10002 data/editorial/ /srv/app/data/editorial/
COPY --chown=10002:10002 reports/basket03-mvp-testing/ /srv/app/reports/basket03-mvp-testing/
COPY --chown=10002:10002 provider_profiles.py /srv/app/provider_profiles.py
COPY --chown=10002:10002 request_ledger.py /srv/app/request_ledger.py

USER 10002:10002
WORKDIR /srv/app
EXPOSE 8000
CMD ["uvicorn", "apps.api.app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
