output "service_url" {
  value       = google_cloud_run_v2_service.app.uri
  description = "The Raccord product. Public only when public_demo=true."
}

output "agent_staging_bucket" {
  value       = "gs://${google_storage_bucket.agent_staging.name}"
  description = "Staging bucket consumed by tools/deploy_agent_engine.py."
}

output "mcp_gateway_url" {
  value       = "${google_cloud_run_v2_service.mcp.uri}/mcp"
  description = "Authenticated official Grafana MCP endpoint used by Raccord and Agent Engine."
}

output "probe_findings_topic" {
  value       = google_pubsub_topic.probe_findings.id
  description = "Topic receiving de-identified live probe summaries."
}

output "analytics_table" {
  value       = "${var.project_id}.${google_bigquery_dataset.analytics.dataset_id}.${google_bigquery_table.incident_outcomes.table_id}"
  description = "De-identified incident outcome table written by the application."
}

output "app_service_account" {
  value       = google_service_account.app.email
  description = "Identity that holds the approval signing key and the Grafana token."
}

output "reasoning_service_account" {
  value       = google_service_account.reasoning.email
  description = "Reasoning identity: MCP/observability credentials only; no approval key, Grafana token, or executor access."
}

output "evidence_bucket" {
  value       = google_storage_bucket.evidence.name
  description = "Versioned, create-only bucket holding incident evidence and the hash-chained audit trail."
}

output "next_steps" {
  value       = <<-EOT
    1. Deploy the managed reasoning plane:
         python tools/deploy_agent_engine.py \
           --staging-bucket gs://${google_storage_bucket.agent_staging.name} \
           --service-account ${google_service_account.reasoning.email} \
           --mcp-url ${google_cloud_run_v2_service.mcp.uri}/mcp \
           --mcp-token-secret ${google_secret_manager_secret.mcp_gateway_token.id} \
           ${local.agent_observability_enabled ? "--agento11y-token-secret ${google_secret_manager_secret.grafana_cloud_access_token[0].id}" : ""}
    2. Put the returned resource name in agent_engine_resource and apply again.
    3. Verify the MCP capability resolution before trusting an investigation:
         raccord mcp --transport http
  EOT
  description = "Post-provisioning Agent Engine connection steps."
}
