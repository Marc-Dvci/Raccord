variable "project_id" {
  type        = string
  description = "Google Cloud project that will run Raccord."
}

variable "region" {
  type        = string
  default     = "europe-west1"
  description = "Deployment region. Keep it close to the delivery chain being measured: probe latency is part of the measurement."
}

variable "image" {
  type        = string
  description = "Fully-qualified application image, e.g. europe-west1-docker.pkg.dev/PROJECT/raccord/app:SHA. Pin a digest in production; a moving tag makes an incident record unreproducible."
}

variable "mcp_image" {
  type        = string
  default     = "grafana/mcp-grafana:1.0.0"
  description = "Pinned official Grafana MCP image used behind the authenticated gateway."
}

variable "reasoning_mode" {
  type        = string
  default     = "gemini"
  description = "offline | gemini. The deployment defaults to gemini; the local demo defaults to offline (ADR 0011)."
  validation {
    condition     = contains(["offline", "gemini"], var.reasoning_mode)
    error_message = "reasoning_mode must be offline or gemini."
  }
}

variable "agent_engine_resource" {
  type        = string
  default     = ""
  description = "Fully-qualified Vertex AI Agent Engine resource. Populate after tools/deploy_agent_engine.py returns it."
}

variable "agent_engine_location" {
  type        = string
  default     = "us-central1"
  description = "Region hosting the Agent Engine runtime."
}

variable "agent_deployer_principals" {
  type        = list(string)
  default     = []
  description = "Users/service accounts allowed to stage code and act as the reasoning service account during deployment."
}

variable "gemini_model" {
  type        = string
  default     = "gemini-3.7-flash"
  description = "GA Gemini agentic reasoning model. Set gemini-3.6-flash only for a constrained-region fallback."
}

variable "gemini_location" {
  type        = string
  default     = "global"
  description = "Google Cloud Gemini endpoint location; Gemini 3.7 Flash supports the global endpoint."
}

variable "public_demo" {
  type        = bool
  default     = true
  description = "Expose the credential-free isolated simulator for judges. Set false for operational use behind IAP/internal ingress."
}

variable "min_instances" {
  type        = number
  default     = 0
  description = "Cloud Run minimum instances. Keep at zero while building; set to one for recording and judging."
  validation {
    condition     = var.min_instances >= 0 && var.min_instances <= var.max_instances
    error_message = "min_instances must be between zero and max_instances."
  }
}

variable "operator_role_bindings" {
  type        = map(list(string))
  default     = {}
  description = "Production-only mapping of IAP email to Raccord roles. Request bodies never grant roles."
}

variable "approval_signing_key" {
  type        = string
  sensitive   = true
  ephemeral   = true
  description = "HMAC approval key. Written through a write-only provider field and never stored in Terraform state."
}

variable "approval_signing_key_version" {
  type        = number
  default     = 1
  description = "Increment to rotate the write-only approval secret value."
}

variable "grafana_service_account_token" {
  type        = string
  sensitive   = true
  ephemeral   = true
  description = "Least-privilege Grafana token for data reads plus governed annotations/incidents. Never stored in Terraform state."
}

variable "grafana_service_account_token_version" {
  type        = number
  default     = 1
  description = "Increment to rotate the write-only Grafana token value."
}

variable "grafana_url" {
  type        = string
  description = "Grafana stack the agent investigates through, e.g. https://<org>.grafana.net."
  validation {
    condition     = !can(regex("localhost|127\\.0\\.0\\.1", var.grafana_url))
    error_message = "The Cloud Run service cannot reach a localhost Grafana. Point this at the real stack."
  }
}

variable "mcp_gateway_token" {
  type        = string
  sensitive   = true
  ephemeral   = true
  description = "High-entropy bearer token for the MCP gateway, distinct from the Grafana credential."
}

variable "mcp_gateway_token_version" {
  type        = number
  default     = 1
  description = "Increment to rotate the write-only MCP gateway token value."
}

variable "grafana_cloud_otlp_endpoint" {
  type        = string
  default     = ""
  description = "Grafana Cloud OTLP gateway root ending in /otlp. Blank disables cloud telemetry export."
}

variable "grafana_cloud_instance_id" {
  type        = string
  default     = ""
  description = "Grafana Cloud OTLP and Agent Observability tenant/instance ID."
}

variable "grafana_cloud_access_token" {
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = ""
  description = "Grafana Cloud token with sigil, metrics, logs and traces write scopes."
}

variable "grafana_cloud_access_token_version" {
  type        = number
  default     = 1
  description = "Increment to rotate the write-only Grafana Cloud ingest token value."
}

variable "agento11y_endpoint" {
  type        = string
  default     = ""
  description = "Agent Observability API URL from the Grafana Cloud plugin Configuration page."
}

variable "operator_principals" {
  type        = list(string)
  default     = []
  description = "Principals allowed to invoke the service, e.g. [\"group:broadcast-ops@studio.example\"]. Empty means nobody but the load balancer - which is the safe default, not an oversight."
}

variable "max_instances" {
  type        = number
  default     = 1
  description = "Upper bound on Cloud Run instances. Keep at one while SQLite owns live state; scale only after moving state to a shared store."
}

variable "evidence_retention_days" {
  type        = number
  default     = 90
  description = "Days before incident evidence moves to nearline storage. It is never deleted by this configuration: an audit trail with a delete lifecycle rule is not an audit trail."
}
