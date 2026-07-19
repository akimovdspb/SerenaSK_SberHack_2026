# syntax=docker/dockerfile:1.7

FROM docker.io/library/node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS web-build
WORKDIR /src
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
RUN npm ci --ignore-scripts
COPY apps/web/ apps/web/
RUN npm run build --workspace @communication-factory/web

FROM docker.io/library/caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d
RUN setcap -r /usr/bin/caddy \
    && addgroup -S -g 10003 gateway \
    && adduser -S -D -H -u 10003 -G gateway gateway
COPY gateway/Caddyfile /etc/caddy/Caddyfile
COPY --chown=10003:10003 --from=web-build /src/apps/web/dist /srv
USER 10003:10003
EXPOSE 8080
