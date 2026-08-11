# browser-mcp: fronts AgentCore Browser as a StreamableHTTP MCP server.
#
# Browser-LESS by design: chrome-devtools-mcp attaches to the REMOTE AgentCore
# browser over CDP, so no Chromium is bundled. Node-only: the server, the AWS
# SigV4 signing and the MCP plumbing are all one Node app, and the only child
# process is the pinned chrome-devtools-mcp (which is a CLI, not a library).
#
# Pinned base by digest for supply-chain immutability.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293

# We attach to a REMOTE browser over CDP, so puppeteer must NOT download a local
# Chromium (that would be the bulk of the image and is never used in connect mode).
ENV PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NODE_ENV=production

# tini becomes PID 1 so orphaned grandchildren are reaped. Each chrome-devtools-mcp
# child spawns its own telemetry watchdog; when the child exits, that watchdog is
# re-parented to PID 1. Node does not wait() on processes it did not spawn, so as
# PID 1 it left one zombie per session (12 observed after ~11 sessions) which
# accumulate against the container's PID limit until nothing can fork. tini exists
# to do exactly this reaping, and also forwards signals so graceful shutdown is
# unaffected.
RUN apk add --no-cache tini=0.19.0-r3 || apk add --no-cache tini

# Pinned chrome-devtools-mcp installed at build time: no npm fetch at runtime.
ARG CDP_MCP_VERSION=1.6.0
RUN npm install -g "chrome-devtools-mcp@${CDP_MCP_VERSION}" \
    && npm cache clean --force

WORKDIR /app

# Install deps from the lockfile for reproducibility, then drop npm's cache.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force

COPY src ./src

# Non-root.
RUN addgroup -g 10001 -S app && adduser -u 10001 -S -G app app
USER 10001

ENV MCP_PORT=8000 \
    MCP_PATH=/mcp \
    CDP_MCP_VERSION=${CDP_MCP_VERSION}
EXPOSE 8000

# tini as PID 1, Node as its child: tini reaps the orphaned watchdogs.
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "src/server.js"]
