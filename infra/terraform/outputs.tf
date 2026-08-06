output "service_url" {
  value       = google_cloud_run_v2_service.app.uri
  description = "The AccessPulse product. Not publicly invokable: access is via the internal load balancer or IAP."
}

output "app_service_account" {
  value       = google_service_account.app.email
  description = "Identity that holds the approval signing key and the Grafana token."
}

output "reasoning_service_account" {
  value       = google_service_account.reasoning.email
  description = "Identity of the reasoning plane. Holds neither secret and cannot invoke the executor."
}

output "evidence_bucket" {
  value       = google_storage_bucket.evidence.name
  description = "Versioned, create-only bucket holding incident evidence and the hash-chained audit trail."
}

output "next_steps" {
  value = <<-EOT
    1. Set the approval signing key out of band - it must never pass through Terraform state:
         head -c 32 /dev/urandom | xxd -p -c 64 | \
           gcloud secrets versions add ${google_secret_manager_secret.approval_signing_key.secret_id} --data-file=-
    2. Add the Grafana service-account token:
         gcloud secrets versions add ${google_secret_manager_secret.grafana_token.secret_id} --data-file=-
    3. Deploy the reasoning plane separately:
         python tools/deploy_agent_engine.py --staging-bucket gs://${var.project_id}-accesspulse-staging
    4. Verify the MCP capability resolution before trusting an investigation:
         accesspulse mcp --transport http
  EOT
  description = "The steps deliberately left outside Terraform."
}
