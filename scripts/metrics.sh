#!/usr/bin/env bash
#
# metrics.sh — read the durable LLM metrics log from S3 and print a report or
# write the self-contained HTML dashboard.
#
# The deployed app (AWS ECS) persists one metrics record per call_llm to
#   s3://zsg-metrics-<ACCOUNT_ID>/metrics
# via its task role. This helper reads them back from S3 using the same
# `python -m zsg.metrics` CLI the code already ships — there is NO web UI for
# metrics by design; this is the operator read path.
#
# Usage:
#   scripts/metrics.sh                     # text report to stdout
#   scripts/metrics.sh --html              # dashboard -> metrics_dashboard.html
#   scripts/metrics.sh --html out.html     # dashboard -> out.html
#
# First run creates a local .venv with boto3 (+ botocore[crt], needed only
# because the operator's AWS profile uses an SSO/login credential provider;
# prod doesn't need it — the ECS task role supplies plain credentials). The
# venv is reused on later runs.
#
# Requires: the `aws` CLI configured with read access to the bucket (the
# ZsgMetricsS3WriteRead policy, or any role/profile that can list+get it).

set -euo pipefail

# --- config (the live deploy) ----------------------------------------------
# Set ZSG_AWS_ACCOUNT_ID in your shell/profile before running this script
# (aws sts get-caller-identity --query Account --output text). Not hardcoded
# here so this file is safe to publish/commit.
: "${ZSG_AWS_ACCOUNT_ID:?Set ZSG_AWS_ACCOUNT_ID to your AWS account id first}"
BUCKET="zsg-metrics-${ZSG_AWS_ACCOUNT_ID}"
PREFIX="metrics"
REGION="us-east-1"

# --- resolve repo root (this script lives in <root>/scripts) ----------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT/.venv"

# --- ensure a venv with boto3 (+ crt) exists --------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  echo "[metrics] creating $VENV with boto3 (one-time setup)..." >&2
  # Prefer a python that exists; the CommandLineTools one is known-present on
  # this machine, but fall back to python3 on PATH.
  PYBOOT="$(command -v python3 || true)"
  if [ -x /Library/Developer/CommandLineTools/usr/bin/python3 ]; then
    PYBOOT=/Library/Developer/CommandLineTools/usr/bin/python3
  fi
  "$PYBOOT" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet "boto3" "botocore[crt]"
fi

# --- run the metrics CLI against S3 -----------------------------------------
# PYTHONWARNINGS=ignore silences boto3's py3.9 deprecation notice.
export ZSG_METRICS_PATH="s3://${BUCKET}/${PREFIX}"
export AWS_REGION="$REGION"
export PYTHONPATH="$ROOT/src"
export PYTHONWARNINGS="ignore"

if [ "${1:-}" = "--html" ]; then
  OUT="${2:-$ROOT/metrics_dashboard.html}"
  "$VENV/bin/python" -m zsg.metrics --html "$OUT"
else
  "$VENV/bin/python" -m zsg.metrics
fi
