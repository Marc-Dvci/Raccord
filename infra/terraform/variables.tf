variable "project_id" {
  type        = string
  description = "Google Cloud project that will run AccessPulse."
}

variable "region" {
  type        = string
  default     = "europe-west1"
  description = "Deployment region. Keep it close to the delivery chain being measured: probe latency is part of the measurement."
}

variable "image" {
  type        = string
  description = "Fully-qualified application image, e.g. europe-west1-docker.pkg.dev/PROJECT/accesspulse/app:SHA. Pin a digest in production; a moving tag makes an incident record unreproducible."
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

variable "grafana_url" {
  type        = string
  description = "Grafana stack the agent investigates through, e.g. https://<org>.grafana.net."
  validation {
    condition     = !can(regex("localhost|127\\.0\\.0\\.1", var.grafana_url))
    error_message = "The Cloud Run service cannot reach a localhost Grafana. Point this at the real stack."
  }
}

variable "grafana_mcp_url" {
  type        = string
  default     = "https://mcp.grafana.com/mcp"
  description = "Grafana MCP server endpoint. The agent has no other route to operational truth (ADR 0002)."
}

variable "operator_principals" {
  type        = list(string)
  default     = []
  description = "Principals allowed to invoke the service, e.g. [\"group:broadcast-ops@studio.example\"]. Empty means nobody but the load balancer - which is the safe default, not an oversight."
}

variable "max_instances" {
  type        = number
  default     = 10
  description = "Upper bound on Cloud Run instances."
}

variable "evidence_retention_days" {
  type        = number
  default     = 90
  description = "Days before incident evidence moves to nearline storage. It is never deleted by this configuration: an audit trail with a delete lifecycle rule is not an audit trail."
}
