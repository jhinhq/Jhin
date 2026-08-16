# Multi-stage build for the Jhin web UI (Next.js standalone output).
# Build context is the repository root (pnpm workspace).

FROM node:22-alpine AS deps
RUN corepack enable pnpm
WORKDIR /repo
# Hoisted layout: Next.js standalone output-file tracing misses files behind
# pnpm's default symlinked store (e.g. @swc/helpers esm subpaths).
ENV npm_config_node_linker=hoisted
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
RUN pnpm install --frozen-lockfile --filter jhin-web

FROM deps AS build
COPY apps/web apps/web
ENV NEXT_TELEMETRY_DISABLED=1
# Baked into the /api/* rewrite at build time (see next.config.ts).
ENV API_INTERNAL_URL=http://api:8000
RUN pnpm --filter jhin-web build

FROM node:22-alpine AS runtime
RUN addgroup -S jhin && adduser -S jhin -G jhin
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
COPY --from=build --chown=jhin:jhin /repo/apps/web/.next/standalone ./
COPY --from=build --chown=jhin:jhin /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=jhin:jhin /repo/apps/web/public ./apps/web/public
USER jhin
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
