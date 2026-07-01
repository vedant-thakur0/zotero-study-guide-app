# AWS ECS Express Mode Deployment Runbook

Deploy the Zotero Study Guide app to **Amazon ECS Express Mode** from a container image in
**Amazon ECR**. **This is the actual, verified deploy path** — it was run end-to-end on
2026-06-17 and the app is live.

> **History / why ECS Express, not App Runner.** We first targeted **AWS App Runner**, but
> App Runner stopped accepting new customers on 2026-04-30 (AWS now recommends **ECS Express
> Mode** as its successor). The container image is identical either way — only the platform
> wrapper changed.

> **CRITICAL — build for linux/amd64.** ECS Fargate runs **x86_64 (amd64)**. An image built
> on an Apple Silicon (arm64) Mac will crash on ECS with `exec /bin/sh: exec format error`
> (it runs fine locally, so this only surfaces in the cloud). Always cross-build with
> `docker buildx build --platform linux/amd64`. See step 2.

**Prerequisites:** an AWS account with billing, the `aws` CLI configured (`aws configure`),
and Docker (Colima) + the `docker-buildx` plugin installed locally.

Placeholders (real values used in the live deploy shown for reference):
- `<ACCOUNT_ID>` — 12-digit AWS account ID (`aws sts get-caller-identity --query Account --output text`) — live: `<ACCOUNT_ID>`
- `<REGION>` — AWS region — live: `us-east-1`
- `<REPO>` — ECR repository name — live: `zotero-study-guide`
- `<TAG>` — image tag — live: `latest`
- `<SERVICE>` — ECS Express service name — live: `zotero-study-guide`

ECR image URI: `<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/<REPO>:<TAG>`

---

## 1. One-time account setup

### 1.1 IAM roles ECS Express needs (two)

```bash
# Execution role — lets ECS pull from ECR and write logs
cat > /tmp/ecs-tasks-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
aws iam create-role --role-name ecsExpressExecutionRole \
  --assume-role-policy-document file:///tmp/ecs-tasks-trust.json
aws iam attach-role-policy --role-name ecsExpressExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# Infrastructure role — lets ECS manage the ALB / target groups / autoscaling
cat > /tmp/ecs-infra-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
aws iam create-role --role-name ecsExpressInfrastructureRole \
  --assume-role-policy-document file:///tmp/ecs-infra-trust.json
aws iam attach-role-policy --role-name ecsExpressInfrastructureRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
```

### 1.2 ECS service-linked role (one-time, account-level)

```bash
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com || true
```

(If it already exists, the `|| true` swallows the harmless "already exists" error. Without
this role, `create-express-gateway-service` fails with "Unable to assume the service linked
role.")

---

## 2. Build (amd64!) and push the image to ECR

```bash
ACCOUNT_ID=<ACCOUNT_ID>; REGION=<REGION>; REPO=<REPO>; TAG=<TAG>
IMG="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:${TAG}"

# Create the ECR repo (idempotent)
aws ecr create-repository --repository-name "$REPO" --region "$REGION" || true

# Log Docker in to ECR
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# Cross-build for amd64 and push in one step (REQUIRES the docker-buildx plugin)
colima start
docker buildx create --name zsgbuilder --driver docker-container --use 2>/dev/null || docker buildx use zsgbuilder
docker buildx build --platform linux/amd64 -t "$IMG" --push .

# Verify it's amd64
docker manifest inspect "$IMG" | grep -A2 '"platform"'
```

If buildx is missing: `brew install docker-buildx` and link it:
`mkdir -p ~/.docker/cli-plugins && ln -sfn "$(brew --prefix)/opt/docker-buildx/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx`.

---

## 3. Create the ECS Express service

The app serves its UI at `/` (not the Express default `/ping`), so the health check path
**must** be `/`. The container listens on **8080**.

```bash
cat > /tmp/ecs-express.json <<JSON
{
  "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/ecsExpressExecutionRole",
  "infrastructureRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/ecsExpressInfrastructureRole",
  "serviceName": "<SERVICE>",
  "healthCheckPath": "/",
  "primaryContainer": {
    "image": "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/<REPO>:<TAG>",
    "containerPort": 8080,
    "environment": [ { "name": "ZSG_METRICS_PATH", "value": "/tmp/metrics.jsonl" } ]
  },
  "cpu": "1024",
  "memory": "2048"
}
JSON

aws ecs create-express-gateway-service --region <REGION> \
  --cli-input-json file:///tmp/ecs-express.json
```

ECS provisions an ALB and starts the task; the public URL appears under `status.endpoint`.

> **API key model: users paste their own.** No Secrets Manager is wired in. The deployed app
> defaults to **client-mode**, where each user enters their Purdue GenAI key in the Setup tab;
> it is sent per-request in the `/api/v2/llm` body and never stored server-side. If you ever
> want a single shared key instead, add it to Secrets Manager and reference it via the
> container's `secrets` array + a read policy on the execution role.

---

## 4. Get the URL and verify

```bash
SVC_ARN="arn:aws:ecs:<REGION>:<ACCOUNT_ID>:service/default/<SERVICE>"

# Public URL (note: the field is status.endpoint)
aws ecs describe-express-gateway-service --service-arn "$SVC_ARN" --region <REGION> \
  --query 'service.status.endpoint' --output text

URL="https://<the-endpoint>"
curl -s -o /dev/null -w 'GET / -> %{http_code}\n' "$URL/"
curl -s -X POST "$URL/api/v2/parse" -H 'Content-Type: application/json' \
  -d '{"format":"json","content":"{\"annotations\":[]}"}' -w '\n-> %{http_code}\n'
```

Both should return `200`. A fresh deploy shows `503` for a couple of minutes while the task
starts and the ALB health check stabilizes — that is expected, not a failure.

**Live deploy (2026-06-17):** `https://<REDACTED>.ecs.us-east-1.on.aws`
— `GET /` 200, `/api/v2/parse` 200, browser generation verified (24 successful LLM calls), and
the metrics log recorded each call.

---

## 5. Redeploy a new image

```bash
# rebuild amd64 + push (step 2), then:
aws ecs update-express-gateway-service --region <REGION> --cli-input-json file:///tmp/ecs-express.json
```

(`update-express-gateway-service` takes the same `serviceArn` + `primaryContainer` shape; it
does NOT accept `--force-new-deployment`. Re-supplying the config rolls a new task.)

---

## 6. Geddes cutover mapping

This AWS deploy rehearses the eventual on-prem **Geddes** deploy. The container image is
identical; only the platform wrapper changes:

| AWS ECS Express | Geddes equivalent |
|---|---|
| Amazon ECR | Harbor registry (`geddes-registry.rcac.purdue.edu`) |
| `aws ecr get-login-password \| docker login` | `docker login` with a Harbor **robot/deploy token** |
| `docker buildx --platform linux/amd64 --push` | same buildx push to Harbor (Geddes is amd64 too) |
| ECS Express service + managed ALB | Rancher/Kubernetes **Deployment** + **Ingress** |
| execution / infrastructure IAM roles | the Harbor deploy token (pull); K8s needs no per-pod IAM |
| `status.endpoint` URL | Geddes **Ingress** hostname |
| health check path `/`, container port 8080 | same (the image binds `$PORT` / 8080) |

AWS-specific, will **not** transfer: the three IAM roles and the ECS/ALB provisioning model.

---

## Cost — ALB trimmed to 2 AZs

Express Mode fans the managed ALB across **all 6 `us-east-1` availability zones**, and each
zone's load-balancer node holds a **public IPv4 address**. Since 2024-02-01 AWS bills
**$0.005/hr per public IPv4** (~$3.65/mo each), so the default layout ran **7 public IPs**
(6 ALB + 1 Fargate task) ≈ **$25/mo** — the single biggest line on this account's bill,
reported under "Amazon Virtual Private Cloud" (the VPC itself is free; the charge is the IPs).

An ALB only needs **2 AZs**, so the live deploy was trimmed to `us-east-1a` + `us-east-1c`.
**Keep the AZ the task runs in** — find it via the target group's `Target.AvailabilityZone`
(it was `us-east-1c`). Trimming drops 4 public IPs (~$14.6/mo saved); cross-zone load balancing
is on, so the app stays reachable throughout with no downtime.

> **Caveat — this is a manual change *outside* Express Mode.** A future `push_and_deploy.sh`
> / `update-express-gateway-service` redeploy can re-expand the ALB back to all 6 AZs. After a
> redeploy, check the public-IP count; if it has climbed back toward 7, re-run the trim:
> ```bash
> aws ec2 describe-network-interfaces --region us-east-1 \
>   --query 'length(NetworkInterfaces[?Association.PublicIp!=`null`])' --output text   # expect 3
> ```

Subnet IDs are **deploy-specific** — list the current ones with
`aws elbv2 describe-load-balancers --names ecs-express-gateway-alb-c7681f3d --query 'LoadBalancers[0].AvailabilityZones[].[ZoneName,SubnetId]' --output text`.

```bash
ALB_ARN=arn:aws:elasticloadbalancing:us-east-1:<ACCOUNT_ID>:loadbalancer/app/ecs-express-gateway-alb-c7681f3d/0e6bcf9b3167d3d8

# Trim to 2 AZs (us-east-1c = task's AZ, us-east-1a):
aws elbv2 set-subnets --region us-east-1 --load-balancer-arn "$ALB_ARN" \
  --subnets subnet-0e92a98d65650ad9f subnet-0b27c0edca5ab5a47

# Rollback — restore all 6 AZs:
aws elbv2 set-subnets --region us-east-1 --load-balancer-arn "$ALB_ARN" \
  --subnets subnet-065116ff23888e0e6 subnet-083377acb21962974 subnet-0b27c0edca5ab5a47 \
            subnet-0cb8484f1ecae5290 subnet-0e92a98d65650ad9f subnet-0f2064fbc49af2461
```

---

## Notes / known limitations

- **Ephemeral metrics.** `metrics.jsonl` lives at `/tmp` inside the task and is lost on task
  restart/scale events. For durable metrics, mount storage or ship records elsewhere — a
  follow-up, deliberately out of scope for v1.
- **Public access.** The service URL is public. The app has no login; restrict with AWS WAF /
  an allowlist if needed.
- **ECS Exec is off**, so you can't `exec` in to read `/tmp/metrics.jsonl` on the live task;
  run the same image locally to inspect metrics behavior.
