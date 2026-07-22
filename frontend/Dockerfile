# syntax=docker/dockerfile:1
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:20-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-alpine AS run
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000
# Patch base OS packages (libssl3/libcrypto3 → CVE-2026-45447) and remove the
# bundled npm: the standalone server runs `node server.js` and never needs npm,
# whose bundled deps (tar / sigstore / cross-spawn / glob / minimatch) otherwise
# carry HIGH CVEs into the runtime image.
RUN for i in 1 2 3; do \
      apk upgrade --no-cache && break; \
      [ "$i" = 3 ] && exit 1; \
      sleep 3; \
    done \
 && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx
COPY --from=build /app/public ./public
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
EXPOSE 3000
USER node
CMD ["node", "server.js"]
