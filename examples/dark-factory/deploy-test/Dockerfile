# Dark Factory deploy-test image — runs in the TRUSTED hub deploy-test step.
#
# Content-aware validation of a PR's deployable artifacts:
#   kind=k8s       → kubectl apply into an ephemeral namespace + wait Ready
#   kind=terraform → terraform init -backend=false + validate (+ fmt check)
# Bundles exactly what both paths + the PR-comment upsert need: kubectl,
# terraform, node, git, curl, bash. This is the only Dark Factory image that
# holds K8s access at runtime (via the workflow SA) — never the coder.
FROM public.ecr.aws/docker/library/alpine:3.20

ARG KUBECTL_VERSION=v1.31.0
ARG TERRAFORM_VERSION=1.9.8
ARG TARGETARCH=amd64

RUN apk add --no-cache bash git curl nodejs unzip ca-certificates \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" -o /tmp/tf.zip \
    && unzip -o /tmp/tf.zip -d /usr/local/bin \
    && rm /tmp/tf.zip \
    && kubectl version --client=true 2>/dev/null | head -1 || true \
    && terraform version | head -1

# Non-root; the step needs no privileged access beyond the SA-bound RBAC.
USER 65532
