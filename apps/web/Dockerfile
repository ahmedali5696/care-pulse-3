FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS build

WORKDIR /workspace

RUN npm install --global --ignore-scripts --no-audit --no-fund pnpm@10.34.7
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml .npmrc ./
COPY apps/web/package.json apps/web/package.json
RUN pnpm install --frozen-lockfile

COPY apps/web/ apps/web/
RUN pnpm --filter @carepulse/web build

FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS runtime

WORKDIR /workspace/apps/web
COPY --from=build --chown=node:node /workspace /workspace
USER node

EXPOSE 3000
# Preflight server-only F-03 secrets/policy BEFORE binding the web listener.
# Next instrumentation also validates but can be lazily initialized on request.
CMD ["sh", "-c", "node --experimental-strip-types --input-type=module -e 'import(\"./config.ts\").then((m) => m.loadWebConfig())' && exec node ingress.mjs"]
