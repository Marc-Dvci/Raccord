# Google Cloud + Grafana Cloud release runbook

This runbook is intentionally pinned to Google Cloud project `grafana-506114`.
Do not change the active `gcloud` project globally: every command supplies the
project explicitly, which prevents accidental changes to unrelated projects.

## 1. Collect the two Grafana credentials

Raccord keeps API access and telemetry ingestion separate.

1. In the Grafana stack, create a service account for the official MCP server.
   It needs the read permissions used by the MCP capability table plus
   `annotations:read` and `annotations:write`. A built-in Editor role is the
   fast hackathon setup; fine-grained RBAC is preferred when the stack supports
   it. Save its `glsa_...` token as the Grafana service-account token.
2. In Grafana Cloud, enable Agent Observability and copy the generation endpoint,
   OTLP endpoint, and numeric instance ID from its Configuration page.
3. Create a stack-scoped Cloud Access Policy token with `metrics:write`,
   `logs:write`, `traces:write`, and `sigil:write`. The last scope is the current
   Agent Observability ingestion permission despite the SDK's `agento11y` name.

Never put either token in a `.tfvars` file or shell history. Terraform declares
them ephemeral and writes them to Secret Manager through write-only fields.

## 2. Prepare the non-secret inputs

Copy `infra/terraform/terraform.tfvars.example` to
`infra/terraform/terraform.tfvars`, fill in the Grafana URLs/IDs, and leave
`agent_engine_resource` blank for the first apply. The destination is ignored
by Git.

Authenticate without changing the global project selection:

```powershell
gcloud.cmd auth application-default login
gcloud.cmd projects describe grafana-506114 --format="value(projectId)"
```

Use environment variables for the four secrets. Generate the two Raccord
secrets locally; paste the two Grafana values only into the current terminal:

```powershell
$env:TF_VAR_approval_signing_key = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower()
$env:TF_VAR_mcp_gateway_token = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower()
$env:TF_VAR_grafana_service_account_token = '<GRAFANA_SERVICE_ACCOUNT_TOKEN>'
$env:TF_VAR_grafana_cloud_access_token = '<GRAFANA_CLOUD_ACCESS_POLICY_TOKEN>'
```

## 3. Bootstrap Artifact Registry and build

Terraform owns the API and repository, so create just those resources first:

```powershell
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform apply `
  -target='google_project_service.required' `
  -target='google_artifact_registry_repository.images'
$sha = (git rev-parse --short=12 HEAD).Trim()
$image = "europe-west1-docker.pkg.dev/grafana-506114/raccord/app:$sha"
gcloud.cmd builds submit --project grafana-506114 --region europe-west1 --tag $image .
```

The 20 August release bootstrap is already complete in `grafana-506114`. Its
verified Python 3.12 image is available immutably as:

```text
europe-west1-docker.pkg.dev/grafana-506114/raccord/app@sha256:dbde537862ec5613c52edb56c6c2c50d932d60124d06ddf7eddcec191a3ba882
```

Put `$image` in the ignored `terraform.tfvars`, then apply the whole foundation:

```powershell
terraform -chdir=infra/terraform apply
```

This creates the public judge app, a separately authenticated Cloud Run gateway
with the official `grafana/mcp-grafana:1.0.0` sidecar, create-only evidence
storage, Pub/Sub, BigQuery, Secret Manager, and separate app/MCP/reasoning
identities. Cloud Run is capped at one instance because live workflow state is
SQLite; `min_instances=0` controls cost while preparing the entry.

## 4. Deploy the managed reasoning plane

Read the Terraform outputs. Then set only non-secret runtime configuration in
the current terminal; Agent Engine reads both tokens from Secret Manager:

```powershell
$env:GOOGLE_CLOUD_PROJECT = 'grafana-506114'
$env:GOOGLE_CLOUD_LOCATION = 'global'
$env:GOOGLE_GENAI_USE_VERTEXAI = 'TRUE'
$env:RACCORD_REASONING_MODE = 'gemini'
$env:RACCORD_MCP_TRANSPORT = 'http'
$env:RACCORD_GRAFANA_URL = '<GRAFANA_STACK_URL>'
$env:RACCORD_OTLP_ENDPOINT = '<GRAFANA_OTLP_ENDPOINT>'
$env:RACCORD_OTLP_USERNAME = '<GRAFANA_INSTANCE_ID>'
$env:AGENTO11Y_ENDPOINT = '<AGENT_OBSERVABILITY_ENDPOINT>'
$env:AGENTO11Y_PROTOCOL = 'http'
$env:AGENTO11Y_AUTH_MODE = 'basic'
$env:AGENTO11Y_AUTH_TENANT_ID = '<GRAFANA_INSTANCE_ID>'
terraform -chdir=infra/terraform output -raw next_steps
```

Run the printed `tools/deploy_agent_engine.py` command. Put the returned resource
name into `agent_engine_resource`, then run `terraform apply` again. The app now
calls Gemini through Vertex AI Agent Engine; the managed agent can read only the
MCP gateway and Agent Observability secrets, never the approval key or the raw
Grafana service-account token.

## 5. Produce release evidence

Set `min_instances=1` before recording/judging, apply, then run:

```powershell
$url = terraform -chdir=infra/terraform output -raw service_url
python tools/cloud_smoke.py $url `
  --expect-gemini --expect-agent-engine --expect-agent-observability `
  --expect-cloud-evidence --expect-telemetry `
  --out docs/cloud_smoke_run.json
```

A valid report proves one real run used HTTP Grafana MCP, Gemini/Agent Engine,
Agent Observability, OTLP, Pub/Sub, Cloud Storage, and BigQuery, while reaching
verified recovery with a valid audit chain and no unsafe action. Also capture:

- the Raccord Incident and Agent & MCP views;
- the Grafana dashboard showing Raccord metrics/logs/traces;
- one Agent Observability conversation/trace;
- the Google Cloud Agent Engine and Cloud Run resource pages.

After recording, return `min_instances` to zero to conserve credits.
