# syntax=docker/dockerfile:1.7

# --- Abhängigkeiten / Entwicklung ----------------------------------------------------
FROM node:22-alpine AS deps
ENV PNPM_HOME=/pnpm PATH=/pnpm:$PATH ASTRO_TELEMETRY_DISABLED=1
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile

FROM deps AS dev
EXPOSE 4321
CMD ["sh", "-c", "pnpm install --frozen-lockfile && pnpm dev --port 4321"]

# --- Statischer Build ----------------------------------------------------------------
FROM deps AS build
COPY . .
RUN pnpm build

# --- Auslieferung über nginx (unprivilegiert, Port 8080) -----------------------------
FROM nginxinc/nginx-unprivileged:stable-alpine AS runtime
ARG BUILD_SHA=dev
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
LABEL org.opencontainers.image.source="https://github.com/ex0th/MusicHub" \
      org.opencontainers.image.title="MusicHub Frontend" \
      org.opencontainers.image.revision="${BUILD_SHA}"
