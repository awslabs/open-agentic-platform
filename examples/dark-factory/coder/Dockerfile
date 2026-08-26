# Dark Factory coder image — runs INSIDE the Kata micro-VM sandbox.
#
# Bundles the in-VM agent + the toolchains a coder run needs (git, node, the
# agentic CLIs). This is untrusted-code territory: the image carries no
# credentials — secrets are injected at runtime via projected tmpfs (0400) and
# model access is only through Bifrost. Keep it lean and pinned.
FROM public.ecr.aws/docker/library/node:20-alpine

# Toolchains for build/test across common stacks + git for checkout/push. Also
# aws-cli + zip + curl because the SAME image is reused by the hub-side steps
# (security-agent diff staging, bootstrap) which need those. bash for the scripts.
RUN apk add --no-cache git bash python3 py3-pip go zip curl aws-cli

# ── Agentic engines — BOTH first-class + selectable via CODER_ENGINE ─────────
#   claude → Claude Code CLI (default)   |   kiro → Kiro CLI (headless)
# entrypoint.js branches on the engine; the image carries both so a run can pick
# either without a rebuild.
RUN npm install -g @anthropic-ai/claude-code

# Kiro CLI. Internally distributed via Builder Toolbox (`toolbox install
# kiro-cli`), which isn't available in this base image, so install from the
# platform's pinned artifact URL at build time. Best-effort: the image still
# builds (Claude as default) if the URL is unset/unreachable — CODER_ENGINE=kiro
# then logs a clear error at runtime rather than silently misbehaving.
ARG KIRO_CLI_URL=""
RUN if [ -n "$KIRO_CLI_URL" ]; then \
      echo "installing kiro-cli from $KIRO_CLI_URL" && \
      curl -fsSL "$KIRO_CLI_URL" -o /usr/local/bin/kiro && chmod +x /usr/local/bin/kiro ; \
    else \
      echo "WARN: KIRO_CLI_URL not set — CODER_ENGINE=kiro unavailable until provided at build" ; \
    fi

# ── AWS DevOps Agent — release-readiness review plugin (NOT baked here) ──────
# The DevOps Agent review is invoked via the Claude Code / Kiro plugin (there is
# NO headless code-review API). That plugin installs via the AIM CLI and connects
# to an Agent Space needing a ONE-TIME console setup (Agent Space + repo connect /
# access key) — a connection that can't be baked into an image or performed from
# the credential-less, network-locked VM (no Midway). entrypoint.js therefore
# reports the DevOps review as "not-connected" (never a fake pass) until the
# platform wires the MCP/A2A access-key path from a hub-side step. See docs §6.2.

WORKDIR /app
# entrypoint.js — env-driven Flow B coder (auto-runs on VM start, self-reports
# via GitHub commit status; the df-run Argo workflow polls GitHub for the result).
COPY entrypoint.js ./

# Non-root, matches the SandboxTemplate securityContext (runAsUser 1000).
USER 1000

ENV WORKSPACE=/workspace

# Baked into the Flow A SandboxTemplate as the coder image. On VM start it reads
# the DF_* env injected by the SandboxClaim, implements + tests, pushes the
# branch, runs the DevOps Agent review (if connected), opens the PR, and sets the
# dark-factory/implementation commit status.
CMD ["node", "entrypoint.js"]
