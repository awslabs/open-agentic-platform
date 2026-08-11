# Flow D — lambda-coder image: the dark-factory coder wrapped for the Lambda MicroVM
# snapshot/hook runtime.
#
# FROM the arm64 dark-factory-coder (Lambda MicroVM is ARM_64-only) — it carries
# entrypoint.js + the toolchain (git, node, claude-code). We add ONLY the hook server
# that adapts the one-shot coder to the MicroVM lifecycle (see hook-server.js): the
# /run hook background-spawns entrypoint.js with USE_BEDROCK=1 so the coder calls
# Bedrock directly via the MicroVM execution role — no Bifrost / EKS-network path.
#
# The image is built by Lambda from a code-artifact ZIP (this Dockerfile + hook-server.js)
# in S3 — NOT pushed to ECR as a normal image. The base coder image IS pulled from ECR
# during that build (the MicrovmImage build role keeps ecr:Get*).
# ⚠️ THIS ACCOUNT ID CANNOT BE TEMPLATED — it must be edited by hand per account.
# Everywhere else in this repo an ECR URI is injected from the cluster's aws_account_id
# annotation (gitops/addons/registry/sandbox.yaml), but that is impossible here: the
# MicrovmImage CRD's `spec.codeArtifact` has EXACTLY ONE property, `uri`. There is no
# build-args field (spec.environmentVariables is RUNTIME env for the VM, not docker build
# args), so nothing in Helm, the registry, or the CR can reach inside this ZIP. Whatever
# this default says is what Lambda pulls. Verified against the live CRD 2026-08-11.
#
# It previously named 940019131157 (the AUTHORING account), which no other account can
# pull from: cross-account ECR needs BOTH an identity grant AND a repository policy on
# the far side, and we hold no credentials there (confirmed: AccessDeniedException on
# ecr:DescribeImages, "no resource-based policy allows"). The build role's
# ecr:BatchGetImage on "*" is only the identity half, so it did NOT help.
#
# The durable fix is a templated packaging step that renders this line from
# aws_account_id at ZIP-assembly time. No such script exists yet — this image and the
# artifact ZIP have only ever been built by hand, which is the root cause of this whole
# drift class. Until then: EDIT THIS LINE when deploying to a new account, and rebuild
# the base with `docker buildx build --platform linux/arm64` (Lambda MicroVM is
# ARM_64-only; see the arch note above).
ARG CODER_IMAGE=259732043995.dkr.ecr.us-west-2.amazonaws.com/dark-factory-coder:v0.2.5-arm64
FROM ${CODER_IMAGE}

WORKDIR /app

# The hook server (serves ready/validate/run/suspend/resume/terminate on :8080).
COPY hook-server.js /app/hook-server.js

# Ship the UPDATED coder over the one baked into the ECR base. The base image's
# entrypoint.js predates the USE_BEDROCK branch; overlaying it here means the
# MicrovmImage build (which pulls the ECR base) gets the Bedrock-capable coder
# WITHOUT a separate ECR rebuild+push. Keep in sync with examples/dark-factory/coder/entrypoint.js.
COPY entrypoint.js /app/entrypoint.js

# Lambda MicroVM snapshots the process started here. hooks.port on the MicrovmImage
# must match this (8080). The server stays up (long-running) so the VM isn't idle-
# suspended mid coder-run — the Microvm idlePolicy.maxIdleDurationSeconds is set
# longer than a coder run.
EXPOSE 8080
CMD ["node", "/app/hook-server.js"]
