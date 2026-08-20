/**
 * Raccord on Google Cloud.
 *
 * The whole demonstration runs locally with no cloud account (ADR 0011), so this
 * is the deployment path, not a prerequisite. What it provisions follows the
 * trust boundary in docs/THREAT_MODEL.md exactly:
 *
 *   - the application (deterministic core, policy, approvals, executor, probes)
 *     runs on Cloud Run under its own service account
 *   - the approval signing key lives in Secret Manager and is readable by one
 *     identity - no key on disk, ever
 *   - the Grafana service-account token is a separate secret with a separate
 *     accessor, because it grants a different thing
 *   - the reasoning plane is deployed separately to Agent Engine and can read
 *     only MCP/telemetry credentials, never the approval signing key
 *
 * terraform init && terraform plan -var project_id=<project>
 */

terraform {
  required_version = ">= 1.10"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  grafana_cloud_ingest_enabled = (
    var.grafana_cloud_otlp_endpoint != "" &&
    var.grafana_cloud_instance_id != ""
  )
  agent_observability_enabled = local.grafana_cloud_ingest_enabled && var.agento11y_endpoint != ""
}

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------

resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "pubsub.googleapis.com",
    "bigquery.googleapis.com",
    "storage.googleapis.com",
    "cloudtrace.googleapis.com",
    "monitoring.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

# The application identity. Deliberately narrow: it may read its two secrets,
# publish evidence, and write telemetry. It may not administer anything.
resource "google_service_account" "app" {
  account_id   = "raccord-app"
  display_name = "Raccord application"
  description  = "Runs the deterministic core, policy engine, approvals and executor."
}

# The reasoning plane runs as a different identity. It can call Vertex AI and
# read only its MCP gateway and observability credentials; it never receives
# the Grafana API token or approval key.
resource "google_service_account" "reasoning" {
  account_id   = "raccord-reasoning"
  display_name = "Raccord reasoning plane"
  description  = "Gemini synthesis and communications. Cannot act on the environment."
}

resource "google_project_iam_member" "reasoning_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.reasoning.email}"
}

resource "google_service_account" "mcp" {
  account_id   = "raccord-mcp"
  display_name = "Raccord Grafana MCP gateway"
  description  = "Runs the authenticated proxy and official grafana/mcp-grafana sidecar."
}

resource "google_service_account_iam_member" "reasoning_deployer" {
  for_each           = toset(var.agent_deployer_principals)
  service_account_id = google_service_account.reasoning.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.key
}

# Cloud Run queries the separately deployed Agent Engine. The executor remains
# unreachable from that reasoning identity; the direction is app -> reasoning.
resource "google_project_iam_member" "app_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_trace" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_metrics" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

# The HMAC key that signs approval tokens. Whoever holds this can mint an
# approval, so exactly one identity can read it and nothing writes it here -
# the value is set out of band, deliberately, so it never passes through
# Terraform state.
resource "google_secret_manager_secret" "approval_signing_key" {
  secret_id = "raccord-approval-signing-key"
  replication {
    auto {}
  }
  labels = {
    sensitivity = "critical"
    purpose     = "approval-integrity"
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "approval_key_accessor" {
  secret_id = google_secret_manager_secret.approval_signing_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_secret_manager_secret_version" "approval_signing_key" {
  secret                 = google_secret_manager_secret.approval_signing_key.id
  secret_data_wo         = var.approval_signing_key
  secret_data_wo_version = var.approval_signing_key_version
}

resource "google_secret_manager_secret" "grafana_token" {
  secret_id = "raccord-grafana-service-account-token"
  replication {
    auto {}
  }
  labels = {
    sensitivity = "high"
    purpose     = "grafana-mcp"
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "grafana_token_accessor" {
  secret_id = google_secret_manager_secret.grafana_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_secret_manager_secret_iam_member" "mcp_grafana_token_accessor" {
  secret_id = google_secret_manager_secret.grafana_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mcp.email}"
}

resource "google_secret_manager_secret_version" "grafana_token" {
  secret                 = google_secret_manager_secret.grafana_token.id
  secret_data_wo         = var.grafana_service_account_token
  secret_data_wo_version = var.grafana_service_account_token_version
}

resource "google_secret_manager_secret" "mcp_gateway_token" {
  secret_id = "raccord-mcp-gateway-token"
  replication {
    auto {}
  }
  labels = {
    sensitivity = "high"
    purpose     = "mcp-client-authentication"
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "mcp_gateway_token" {
  secret                 = google_secret_manager_secret.mcp_gateway_token.id
  secret_data_wo         = var.mcp_gateway_token
  secret_data_wo_version = var.mcp_gateway_token_version
}

resource "google_secret_manager_secret_iam_member" "app_mcp_gateway_token_accessor" {
  secret_id = google_secret_manager_secret.mcp_gateway_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_secret_manager_secret_iam_member" "reasoning_mcp_gateway_token_accessor" {
  secret_id = google_secret_manager_secret.mcp_gateway_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.reasoning.email}"
}

resource "google_secret_manager_secret_iam_member" "mcp_gateway_token_accessor" {
  secret_id = google_secret_manager_secret.mcp_gateway_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mcp.email}"
}

resource "google_secret_manager_secret" "grafana_cloud_access_token" {
  count     = local.grafana_cloud_ingest_enabled ? 1 : 0
  secret_id = "raccord-grafana-cloud-ingest-token"
  replication {
    auto {}
  }
  labels = {
    sensitivity = "high"
    purpose     = "grafana-cloud-otlp-agento11y"
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "grafana_cloud_access_token" {
  count                  = local.grafana_cloud_ingest_enabled ? 1 : 0
  secret                 = google_secret_manager_secret.grafana_cloud_access_token[0].id
  secret_data_wo         = var.grafana_cloud_access_token
  secret_data_wo_version = var.grafana_cloud_access_token_version
}

resource "google_secret_manager_secret_iam_member" "app_grafana_cloud_access_token" {
  count     = local.grafana_cloud_ingest_enabled ? 1 : 0
  secret_id = google_secret_manager_secret.grafana_cloud_access_token[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_secret_manager_secret_iam_member" "reasoning_grafana_cloud_access_token" {
  count     = local.agent_observability_enabled ? 1 : 0
  secret_id = google_secret_manager_secret.grafana_cloud_access_token[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.reasoning.email}"
}

# ---------------------------------------------------------------------------
# Evidence and analytics
# ---------------------------------------------------------------------------

# Incident evidence: write-once, retained for the audit period, and versioned so
# a hash-chained audit trail cannot be quietly rewritten.
resource "google_storage_bucket" "evidence" {
  name                        = "${var.project_id}-raccord-evidence"
  location                    = var.region
  uniform_bucket_level_access = true
  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      age = var.evidence_retention_days
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
  depends_on = [google_project_service.required]
}

# Agent Engine stages the packaged ADK application here during deployment.
resource "google_storage_bucket" "agent_staging" {
  name                        = "${var.project_id}-raccord-staging"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  depends_on                  = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "agent_staging_deployer" {
  for_each = toset(var.agent_deployer_principals)
  bucket   = google_storage_bucket.agent_staging.name
  role     = "roles/storage.objectAdmin"
  member   = each.key
}

resource "google_storage_bucket_iam_member" "evidence_writer" {
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectCreator" # create, never overwrite or delete
  member = "serviceAccount:${google_service_account.app.email}"
}

# Aggregate analytics only. Nothing in this dataset identifies a viewer: the
# schema has no identifier column, and k-anonymity suppression is applied before
# a row is written (docs/PRIVACY.md).
resource "google_bigquery_dataset" "analytics" {
  dataset_id                 = "raccord_analytics"
  location                   = var.region
  delete_contents_on_destroy = false
  labels = {
    contains_personal_data = "no"
    k_anonymity_threshold  = "50"
  }
  depends_on = [google_project_service.required]
}

resource "google_bigquery_table" "incident_outcomes" {
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "incident_outcomes"
  deletion_protection = true
  schema = jsonencode([
    { name = "incident_id", type = "STRING", mode = "REQUIRED" },
    { name = "event_id", type = "STRING", mode = "REQUIRED" },
    { name = "recorded_at", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "severity", type = "STRING", mode = "REQUIRED" },
    { name = "root_cause", type = "STRING", mode = "REQUIRED" },
    { name = "diagnosis_confidence", type = "FLOAT", mode = "REQUIRED" },
    { name = "affected_sessions", type = "INTEGER", mode = "REQUIRED" },
    { name = "protected_sessions", type = "INTEGER", mode = "REQUIRED" },
    { name = "time_to_recovery_s", type = "FLOAT", mode = "NULLABLE" },
    { name = "outage_seconds", type = "FLOAT", mode = "NULLABLE" },
    { name = "diagnosis_correct", type = "BOOLEAN", mode = "NULLABLE" },
    { name = "audit_chain_valid", type = "BOOLEAN", mode = "REQUIRED" },
  ])
}

resource "google_bigquery_dataset_iam_member" "app_analytics_writer" {
  dataset_id = google_bigquery_dataset.analytics.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.app.email}"
}

# The real-time event plane: probe findings and SLO evaluations.
resource "google_pubsub_topic" "probe_findings" {
  name                       = "raccord-probe-findings"
  message_retention_duration = "86400s"
  depends_on                 = [google_project_service.required]
}

resource "google_pubsub_topic_iam_member" "app_publisher" {
  topic  = google_pubsub_topic.probe_findings.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.app.email}"
}

# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "images" {
  repository_id = "raccord"
  location      = var.region
  format        = "DOCKER"
  description   = "Raccord application images"
  depends_on    = [google_project_service.required]
}

# ---------------------------------------------------------------------------
# Authenticated official Grafana MCP gateway
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "mcp" {
  name     = "raccord-grafana-mcp"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.mcp.email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = 1
    }

    # Cloud Run terminates TLS at this ingress/auth proxy.
    containers {
      name    = "gateway"
      image   = var.image
      command = ["python", "-m", "uvicorn"]
      args    = ["raccord.mcp_gateway:app", "--host", "0.0.0.0", "--port", "8080"]

      ports {
        container_port = 8080
      }

      env {
        name  = "RACCORD_MCP_UPSTREAM_URL"
        value = "http://127.0.0.1:8000/mcp"
      }
      env {
        name  = "RACCORD_MCP_UPSTREAM_HEALTH_URL"
        value = "http://127.0.0.1:8000/healthz"
      }
      env {
        name = "RACCORD_MCP_GATEWAY_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.mcp_gateway_token.secret_id
            version = google_secret_manager_secret_version.mcp_gateway_token.version
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 3
        period_seconds        = 3
        failure_threshold     = 20
      }
      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds = 30
      }
    }

    # The partner's official server owns the Grafana credential and has no
    # direct public port. The proxy strips its independent bearer token.
    containers {
      name  = "mcp-grafana"
      image = var.mcp_image
      args = [
        "-t", "streamable-http",
        "--address", "0.0.0.0:8000",
        "--allowed-hosts", "*",
      ]

      env {
        name  = "GRAFANA_URL"
        value = var.grafana_url
      }
      env {
        name = "GRAFANA_SERVICE_ACCOUNT_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.grafana_token.secret_id
            version = google_secret_manager_secret_version.grafana_token.version
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.mcp_grafana_token_accessor,
    google_secret_manager_secret_iam_member.mcp_gateway_token_accessor,
    google_secret_manager_secret_version.grafana_token,
    google_secret_manager_secret_version.mcp_gateway_token,
  ]
}

# Cloud Run admits the request; the application gateway then authenticates the
# caller. Agent Engine can therefore use a simple secret without Grafana access.
resource "google_cloud_run_v2_service_iam_member" "mcp_public_edge" {
  name     = google_cloud_run_v2_service.mcp.name
  location = google_cloud_run_v2_service.mcp.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service" "app" {
  name     = "raccord"
  location = var.region
  # The contest deployment is an isolated simulator and may be public. Set
  # public_demo=false for real operations; that changes both ingress and IAM.
  ingress = var.public_demo ? "INGRESS_TRAFFIC_ALL" : "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    service_account = google_service_account.app.email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
        cpu_idle = false # the probe fleet runs between requests
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "RACCORD_REASONING_MODE"
        value = var.reasoning_mode
      }
      env {
        name  = "RACCORD_AGENT_ENGINE_RESOURCE"
        value = var.agent_engine_resource
      }
      env {
        name  = "RACCORD_GRAFANA_URL"
        value = var.grafana_url
      }
      env {
        name  = "RACCORD_MCP_TRANSPORT"
        value = "http"
      }
      env {
        name  = "RACCORD_MCP_HTTP_URL"
        value = "${google_cloud_run_v2_service.mcp.uri}/mcp"
      }
      env {
        name  = "RACCORD_MCP_GRAFANA_URL"
        value = ""
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.gemini_location
      }
      env {
        name  = "RACCORD_GEMINI_MODEL"
        value = var.gemini_model
      }
      env {
        name  = "RACCORD_GEMINI_LOCATION"
        value = var.gemini_location
      }
      env {
        name  = "RACCORD_AGENT_ENGINE_LOCATION"
        value = var.agent_engine_location
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "RACCORD_EVIDENCE_BUCKET"
        value = google_storage_bucket.evidence.name
      }
      env {
        name  = "RACCORD_PROBE_FINDINGS_TOPIC"
        value = google_pubsub_topic.probe_findings.id
      }
      env {
        name  = "RACCORD_ANALYTICS_DATASET"
        value = google_bigquery_dataset.analytics.dataset_id
      }
      env {
        name  = "RACCORD_DEMO_MODE"
        value = tostring(var.public_demo)
      }
      env {
        name  = "RACCORD_OPERATOR_ROLE_BINDINGS_JSON"
        value = jsonencode(var.operator_role_bindings)
      }
      env {
        name  = "RACCORD_EXPORT_TELEMETRY"
        value = tostring(local.grafana_cloud_ingest_enabled)
      }
      env {
        name  = "RACCORD_OTLP_ENDPOINT"
        value = var.grafana_cloud_otlp_endpoint
      }
      env {
        name  = "RACCORD_OTLP_USERNAME"
        value = var.grafana_cloud_instance_id
      }
      env {
        name  = "AGENTO11Y_ENDPOINT"
        value = var.agento11y_endpoint
      }
      env {
        name  = "AGENTO11Y_PROTOCOL"
        value = local.agent_observability_enabled ? "http" : "none"
      }
      env {
        name  = "AGENTO11Y_AUTH_MODE"
        value = local.agent_observability_enabled ? "basic" : "none"
      }
      env {
        name  = "AGENTO11Y_AUTH_TENANT_ID"
        value = var.grafana_cloud_instance_id
      }

      env {
        name = "RACCORD_APPROVAL_SIGNING_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.approval_signing_key.secret_id
            version = google_secret_manager_secret_version.approval_signing_key.version
          }
        }
      }
      env {
        name = "RACCORD_GRAFANA_SERVICE_ACCOUNT_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.grafana_token.secret_id
            version = google_secret_manager_secret_version.grafana_token.version
          }
        }
      }
      env {
        name = "RACCORD_MCP_BEARER_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.mcp_gateway_token.secret_id
            version = google_secret_manager_secret_version.mcp_gateway_token.version
          }
        }
      }
      dynamic "env" {
        for_each = local.grafana_cloud_ingest_enabled ? [1] : []
        content {
          name = "RACCORD_OTLP_AUTH_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.grafana_cloud_access_token[0].secret_id
              version = google_secret_manager_secret_version.grafana_cloud_access_token[0].version
            }
          }
        }
      }
      dynamic "env" {
        for_each = local.agent_observability_enabled ? [1] : []
        content {
          name = "AGENTO11Y_AUTH_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.grafana_cloud_access_token[0].secret_id
              version = google_secret_manager_secret_version.grafana_cloud_access_token[0].version
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/readyz"
        }
        initial_delay_seconds = 5
        failure_threshold     = 10
      }
      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds = 30
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.approval_key_accessor,
    google_secret_manager_secret_iam_member.grafana_token_accessor,
    google_secret_manager_secret_iam_member.app_mcp_gateway_token_accessor,
    google_secret_manager_secret_iam_member.app_grafana_cloud_access_token,
    google_secret_manager_secret_version.approval_signing_key,
    google_secret_manager_secret_version.grafana_token,
    google_secret_manager_secret_version.mcp_gateway_token,
    google_cloud_run_v2_service_iam_member.mcp_public_edge,
    google_bigquery_table.incident_outcomes,
  ]
}

# Explicitly *not* granting roles/run.invoker to allUsers. Access is via IAP or
# an internal load balancer, and the omission is the point.
resource "google_cloud_run_v2_service_iam_member" "operators" {
  for_each = toset(var.operator_principals)
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.invoker"
  member   = each.key
}

resource "google_cloud_run_v2_service_iam_member" "public_demo" {
  count    = var.public_demo ? 1 : 0
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
