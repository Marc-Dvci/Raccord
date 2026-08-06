/**
 * AccessPulse on Google Cloud.
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
 *   - the reasoning plane is deployed separately to Agent Engine
 *     (tools/deploy_agent_engine.py) and holds neither secret
 *
 * terraform init && terraform plan -var project_id=<project>
 */

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
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
  account_id   = "accesspulse-app"
  display_name = "AccessPulse application"
  description  = "Runs the deterministic core, policy engine, approvals and executor."
}

# The reasoning plane runs as a different identity and holds neither secret.
# It can call Vertex AI and nothing else.
resource "google_service_account" "reasoning" {
  account_id   = "accesspulse-reasoning"
  display_name = "AccessPulse reasoning plane"
  description  = "Gemini synthesis and communications. Cannot act on the environment."
}

resource "google_project_iam_member" "reasoning_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.reasoning.email}"
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
  secret_id = "accesspulse-approval-signing-key"
  replication {
    auto {}
  }
  labels = {
    sensitivity = "critical"
    purpose     = "approval-integrity"
  }
}

resource "google_secret_manager_secret_iam_member" "approval_key_accessor" {
  secret_id = google_secret_manager_secret.approval_signing_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_secret_manager_secret" "grafana_token" {
  secret_id = "accesspulse-grafana-service-account-token"
  replication {
    auto {}
  }
  labels = {
    sensitivity = "high"
    purpose     = "grafana-mcp"
  }
}

resource "google_secret_manager_secret_iam_member" "grafana_token_accessor" {
  secret_id = google_secret_manager_secret.grafana_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

# ---------------------------------------------------------------------------
# Evidence and analytics
# ---------------------------------------------------------------------------

# Incident evidence: write-once, retained for the audit period, and versioned so
# a hash-chained audit trail cannot be quietly rewritten.
resource "google_storage_bucket" "evidence" {
  name                        = "${var.project_id}-accesspulse-evidence"
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
      type = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
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
  dataset_id                 = "accesspulse_analytics"
  location                   = var.region
  delete_contents_on_destroy = false
  labels = {
    contains_personal_data = "no"
    k_anonymity_threshold  = "50"
  }
}

# The real-time event plane: probe findings and SLO evaluations.
resource "google_pubsub_topic" "probe_findings" {
  name = "accesspulse-probe-findings"
  message_retention_duration = "86400s"
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
  repository_id = "accesspulse"
  location      = var.region
  format        = "DOCKER"
  description   = "AccessPulse application images"
}

resource "google_cloud_run_v2_service" "app" {
  name     = "accesspulse"
  location = var.region
  # No unauthenticated access. The demo binds to localhost with no auth because
  # it ships with no credentials; a deployment must not (THREAT_MODEL section 4.10).
  ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    service_account = google_service_account.app.email
    scaling {
      min_instance_count = 1 # a live event cannot wait for a cold start
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
        name  = "AP_REASONING_MODE"
        value = var.reasoning_mode
      }
      env {
        name  = "AP_GRAFANA_URL"
        value = var.grafana_url
      }
      env {
        name  = "AP_MCP_TRANSPORT"
        value = "http"
      }
      env {
        name  = "AP_MCP_HTTP_URL"
        value = var.grafana_mcp_url
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "AP_EVIDENCE_BUCKET"
        value = google_storage_bucket.evidence.name
      }

      env {
        name = "AP_APPROVAL_SIGNING_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.approval_signing_key.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "AP_GRAFANA_SERVICE_ACCOUNT_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.grafana_token.secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
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
