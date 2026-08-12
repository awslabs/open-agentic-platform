# code-interpreter-mcp: fronts AgentCore Code Interpreter as a StreamableHTTP MCP server.
#
# Pure Node, and unlike browser-mcp there is no child process at all: the AWS SDK is the
# whole client, because the Code Interpreter is an ordinary request/response API while
# AgentCore Browser exposes only a SigV4-signed CDP WebSocket.
#
# Three things the browser image needs and this one deliberately does NOT:
#
#   - tini as PID 1. That exists in browser-mcp to reap orphaned grandchildren: each
#     chrome-devtools-mcp child spawns a telemetry watchdog that gets re-parented to PID 1
#     when the child exits, and Node does not wait() on processes it did not spawn, so
#     zombies accumulated (12 observed after ~11 sessions). This server spawns nothing, so
#     Node as PID 1 is correct. Add tini back the moment a child process appears here.
#   - a globally installed chrome-devtools-mcp CLI.
#   - the PUPPETEER_SKIP_DOWNLOAD guards that stop a local Chromium from being bundled.
#
# Pinned base by digest for supply-chain immutability. Same digest browser-mcp uses.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NODE_ENV=production

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
    MCP_PATH=/mcp
EXPOSE 8000

CMD ["node", "src/server.js"]
