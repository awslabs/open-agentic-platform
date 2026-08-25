#!/bin/bash
set -e

echo "=========================================="
echo "Agent Platform GitOps Bootstrap"
echo "=========================================="
echo ""

# Defaults
CLUSTER_NAME="${EKS_CLUSTER_NAME:-dev}"
AWS_REGION="${AWS_REGION:-us-west-2}"
REPO_URL="${REPO_URL:-https://github.com/aws-samples/sample-open-agentic-platform}"
REPO_REVISION="${REPO_REVISION:-main}"
# Reference model: the appset-chart generator is pulled from the platform repo
# (appmod-blueprints), not vendored in this repo.
PLATFORM_REPO_URL="${PLATFORM_REPO_URL:-https://github.com/aws-samples/appmod-blueprints}"
PLATFORM_REPO_REVISION="${PLATFORM_REPO_REVISION:-feature/agent-platform}"
PROJECT_NAME="${PROJECT_NAME:-agent-core}"
TOFU_ROLE_ARN="${TOFU_CONTROLLER_ROLE_ARN:-}"
MCP_IMAGE="${AGENT_CORE_MCP_IMAGE:-}"
TF_REPO_URL="${AGENT_CORE_TERRAFORM_REPO_URL:-https://github.com/aws-samples/sample-open-agentic-platform}"
TF_REPO_REVISION="${AGENT_CORE_TERRAFORM_REPO_REVISION:-main}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

echo "Configuration:"
echo "  Cluster:        $CLUSTER_NAME"
echo "  Region:         $AWS_REGION"
echo "  Repo URL:       $REPO_URL"
echo "  Revision:       $REPO_REVISION"
echo "  Platform Repo:  $PLATFORM_REPO_URL@$PLATFORM_REPO_REVISION"
echo "  Project:        $PROJECT_NAME"
echo "  Environment:    $ENVIRONMENT"
echo "  Terraform Repo: $TF_REPO_URL"
echo ""

# Step 1: Check prerequisites
echo "Step 1: Checking prerequisites..."

if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl not found"
    exit 1
fi

if ! command -v helm &> /dev/null; then
    echo "❌ helm not found"
    exit 1
fi

if ! kubectl cluster-info &> /dev/null; then
    echo "❌ Cannot connect to cluster"
    exit 1
fi

echo "✅ Prerequisites met"
echo ""

# Step 2: Install ArgoCD if not present
echo "Step 2: Checking ArgoCD..."
if ! kubectl get namespace argocd &> /dev/null; then
    echo "Installing ArgoCD..."
    kubectl create namespace argocd
    kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
    kubectl wait --for=condition=available --timeout=300s deployment/argocd-server -n argocd
    echo "✅ ArgoCD installed"
else
    echo "✅ ArgoCD already installed"
fi
echo ""

# Step 3: Install EKS Pod Identity Agent if not present
echo "Step 3: Checking EKS Pod Identity Agent..."
ADDON_STATUS=$(aws eks describe-addon --cluster-name "$CLUSTER_NAME" --addon-name eks-pod-identity-agent --region "$AWS_REGION" --query 'addon.status' --output text 2>/dev/null || echo "NOT_FOUND")
if [ "$ADDON_STATUS" = "NOT_FOUND" ]; then
    echo "Installing EKS Pod Identity Agent..."
    aws eks create-addon \
        --cluster-name "$CLUSTER_NAME" \
        --addon-name eks-pod-identity-agent \
        --region "$AWS_REGION"
    echo "Waiting for addon to be active..."
    aws eks wait addon-active \
        --cluster-name "$CLUSTER_NAME" \
        --addon-name eks-pod-identity-agent \
        --region "$AWS_REGION"
    echo "✅ Pod Identity Agent installed"
else
    echo "✅ Pod Identity Agent already active ($ADDON_STATUS)"
fi
echo ""

# Step 4: Create/update ArgoCD cluster secret with labels and annotations
echo "Step 4: Configuring ArgoCD cluster secret..."

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: in-cluster
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: cluster
    fleet_member: control-plane
    environment: ${ENVIRONMENT}
    tenant: default
    enable_agent_platform: "true"
    enable_bifrost: "true"
    enable_langfuse: "true"
    enable_otel_collector: "true"
    enable_monitoring: "true"
    enable_agent_core: "true"
    enable_agent_gateway: "true"
    enable_oam_components: "true"
  annotations:
    aws_region: "${AWS_REGION}"
    aws_account_id: "${AWS_ACCOUNT_ID}"
    aws_cluster_name: "${CLUSTER_NAME}"
    tofu_controller_role_arn: "${TOFU_ROLE_ARN}"
    agent_core_project_name: "${PROJECT_NAME}"
    agent_core_network_mode: "PUBLIC"
    agent_core_mcp_image: "${MCP_IMAGE}"
    agent_core_mcp_image_tag: "latest"
    agent_core_terraform_repo_url: "${TF_REPO_URL}"
    agent_core_terraform_repo_revision: "${TF_REPO_REVISION}"
type: Opaque
stringData:
  name: in-cluster
  server: https://kubernetes.default.svc
  config: |
    {
      "tlsClientConfig": {
        "insecure": false
      }
    }
EOF

echo "✅ Cluster secret configured"
echo ""

# Step 5: Deploy the agent-platform bootstrap Application
echo "Step 5: Deploying agent-platform bootstrap..."
# The appset-chart generator is sourced from the PLATFORM repo (reference model);
# this repo supplies the registry/ value files. Repo coordinates are injected as
# literals via envsubst. HUB_CLUSTER_NAME must be the ArgoCD-registered cluster
# name — "in-cluster" in this standalone flow (see the secret above), not the EKS
# name — so the Application's destination matches.
REPO_URL="$REPO_URL" REVISION="$REPO_REVISION" BASEPATH="gitops/addons/" \
  HUB_CLUSTER_NAME="in-cluster" \
  PLATFORM_REPO_URL="$PLATFORM_REPO_URL" PLATFORM_REPO_REVISION="$PLATFORM_REPO_REVISION" \
  envsubst '${REPO_URL} ${REVISION} ${BASEPATH} ${HUB_CLUSTER_NAME} ${PLATFORM_REPO_URL} ${PLATFORM_REPO_REVISION}' \
  < gitops/bootstrap/agent-platform-app.yaml | kubectl apply -f -
echo "✅ Agent-platform bootstrap deployed"
echo ""

echo "=========================================="
echo "Bootstrap Complete!"
echo "=========================================="
echo ""
echo "ArgoCD will now automatically:"
echo "  1. Generate ApplicationSets from addons.yaml"
echo "  2. Deploy Flux source-controller"
echo "  3. Deploy Tofu Controller"
echo "  4. Deploy KAgent CRDs and operator"
echo "  5. Deploy LiteLLM gateway + ModelConfig"
echo "  6. Deploy Agent Core infrastructure (Terraform + MCP server + Agent)"
echo "  7. Deploy observability stack (Langfuse, Jaeger)"
echo ""
echo "Monitor progress:"
echo "  kubectl get applicationsets -n argocd"
echo "  kubectl get applications -n argocd"
echo "  kubectl get terraform -n agent-core-infra"
echo ""
echo "Access UIs:"
echo "  ArgoCD:  kubectl port-forward svc/argocd-server -n argocd 8080:443"
echo "  Langfuse: kubectl port-forward -n langfuse svc/langfuse 3000:3000"
echo "  Jaeger:  kubectl port-forward -n jaeger svc/jaeger 16686:16686"
echo ""
