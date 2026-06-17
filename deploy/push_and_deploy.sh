#!/bin/bash
# push_and_deploy.sh — cross-build the image for amd64, push to Amazon ECR, and
# roll the ECS Express Mode service to the new image.
#
# Run the one-time setup in deploy/README.md first (IAM roles, ECR repo, and the
# initial `aws ecs create-express-gateway-service`). This script handles the
# recurring build → push → redeploy loop only.
#
# Usage:
#   ./deploy/push_and_deploy.sh
#
# Environment variables (must be set before running):
#   ACCOUNT_ID    12-digit AWS account ID
#   REGION        AWS region (e.g., us-east-1)
#   REPO          ECR repository name
#   TAG           Image tag (e.g., latest, v1.0.0, or a commit hash)
#   SERVICE       ECS Express service name (used to build the service ARN)
#
# The GenAI API key is NOT handled here — the deployed app uses client-mode,
# where each user pastes their own key in the Setup tab (sent per-request, never
# stored). Nothing secret touches this script or the image.

set -euo pipefail

for var in ACCOUNT_ID REGION REPO TAG SERVICE; do
  if [ -z "${!var+x}" ]; then
    echo "Error: Required environment variable '$var' is not set." >&2
    exit 1
  fi
done

ECR_HOST="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMG="${ECR_HOST}/${REPO}:${TAG}"
SVC_ARN="arn:aws:ecs:${REGION}:${ACCOUNT_ID}:service/default/${SERVICE}"

echo "Account:  $ACCOUNT_ID"
echo "Region:   $REGION"
echo "Image:    $IMG"
echo "Service:  $SVC_ARN"

# ── Log in to ECR ────────────────────────────────────────────────────────────
echo ""
echo "Logging in to ECR..."
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${ECR_HOST}"

# ── Cross-build for amd64 and push (ECS Fargate is x86_64) ────────────────────
# A native arm64 build (Apple Silicon) crashes on ECS with "exec format error".
echo ""
echo "Cross-building for linux/amd64 and pushing..."
docker buildx create --name zsgbuilder --driver docker-container --use 2>/dev/null \
  || docker buildx use zsgbuilder
docker buildx build --platform linux/amd64 -t "${IMG}" --push .

# ── Roll the ECS Express service to the new image ─────────────────────────────
# update-express-gateway-service does NOT accept --force-new-deployment; re-supply
# the service config to trigger a fresh task.
echo ""
echo "Rolling ECS Express service to the new image..."
cat > /tmp/ecs-express-update.json <<JSON
{
  "serviceArn": "${SVC_ARN}",
  "executionRoleArn": "arn:aws:iam::${ACCOUNT_ID}:role/ecsExpressExecutionRole",
  "healthCheckPath": "/",
  "primaryContainer": {
    "image": "${IMG}",
    "containerPort": 8080,
    "environment": [ { "name": "ZSG_METRICS_PATH", "value": "/tmp/metrics.jsonl" } ]
  },
  "cpu": "1024",
  "memory": "2048"
}
JSON
aws ecs update-express-gateway-service --region "${REGION}" \
  --cli-input-json file:///tmp/ecs-express-update.json

echo ""
echo "Deployment triggered. Watch the new task and get the URL with:"
echo "  aws ecs describe-express-gateway-service --service-arn \"${SVC_ARN}\" --region \"${REGION}\" --query 'service.status.endpoint' --output text"
