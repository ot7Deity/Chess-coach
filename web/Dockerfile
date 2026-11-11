# syntax=docker/dockerfile:1
#
# The Chess Coach dashboard, production build. Build context is web/:
#
#   docker build -t chess-coach-web \
#     --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 web
#   docker run --rm -p 3000:3000 chess-coach-web
#
# NEXT_PUBLIC_API_URL is BAKED INTO THE JAVASCRIPT AT BUILD TIME, not read at
# run time: Next inlines NEXT_PUBLIC_* while bundling, so `docker run -e` has no
# effect. And it is fetched by the *browser*, not by this container, so it must
# be the API's address as the user's browser sees it (http://localhost:8000 on
# a laptop), never a compose service name like http://api:8000, which only
# resolves inside the Docker network. A different API address means a rebuild.
#
# Building without it fails, unless WEB_FIXTURES=1 asks for the sample-data
# build on purpose: a silent fixture build would look like a working dashboard
# showing someone else's games.

ARG NODE_IMAGE=node:22.21.1-alpine3.22

# --- deps: install exactly what the lockfile says -----------------------------
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# --- build ----------------------------------------------------------------------
FROM ${NODE_IMAGE} AS build
WORKDIR /app
ARG NEXT_PUBLIC_API_URL=""
ARG WEB_FIXTURES=0
RUN if [ -z "$NEXT_PUBLIC_API_URL" ] && [ "$WEB_FIXTURES" != "1" ]; then \
      echo "NEXT_PUBLIC_API_URL is required (the API as the browser sees it, e.g. http://localhost:8000)." >&2; \
      echo "Pass WEB_FIXTURES=1 to build the sample-data dashboard instead." >&2; \
      exit 1; \
    fi
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_OUTPUT=standalone \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# --- runtime: the traced standalone server, no node_modules, no source ---------
FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
# The standalone server does not serve /_next/static or public/ by itself;
# they are copied next to it, where it looks for them.
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
# The node image's unprivileged user (uid 1000). Nothing here needs root.
USER node
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]
