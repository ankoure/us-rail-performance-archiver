variable "region" {
  type    = string
  default = "us-east-1"
}

variable "aws_profile" {
  type        = string
  default     = null
  description = "AWS named profile for the provider to use (matches the aws CLI). Leave null to use the default credential chain / env vars."
}

variable "landing_bucket" {
  type        = string
  default     = "rail-performance-archiver-landing"
  description = "Prod S3 landing bucket the pollers dual-write to and the rollup reads."
}

variable "landing_retention_days" {
  type        = number
  default     = 7
  description = <<-EOT
    Days to keep landing objects in S3 before lifecycle expiry. The rollup only
    needs yesterday; this is a buffer. Landing is ~21 GiB/day, so this caps the
    transient cost while there's no S3-aware prune yet (that's step 4).
  EOT
}

variable "instance_role_name" {
  type        = string
  description = <<-EOT
    Name of the EC2 instance role attached to the poller box (its instance
    profile). Terraform attaches a landing-bucket write policy to it WITHOUT
    managing the role itself. Find it with:
      aws iam list-instance-profiles --query 'InstanceProfiles[].Roles[].RoleName'
    (deploy/README.md references the profile "rail-archiver-instance").
  EOT
}

# --- 3b: Fargate rollup shadow task --------------------------------------- #

variable "rollup_image" {
  type        = string
  default     = "ghcr.io/ankoure/us-rail-performance-archiver:latest"
  description = "Container image for the rollup task (must be PUBLIC on GHCR for Fargate to pull without creds)."
}

variable "hot_scratch_bucket" {
  type        = string
  default     = "rail-performance-archiver-hot-scratch"
  description = "Scratch bucket the SHADOW rollup wrote parquet to (kept for re-shadowing; the prod task now writes hot_bucket)."
}

# Prod curated buckets — created out-of-band (not Terraform-managed), referenced
# by name. These match config/feeds.yaml s3.hot_bucket / s3.cold_bucket so the
# Fargate rollup ships to the same place the on-box batch did.
variable "hot_bucket" {
  type        = string
  default     = "rail-performance-archiver-hot"
  description = "Prod hot bucket the rollup writes curated parquet to (must match feeds.yaml s3.hot_bucket)."
}

variable "cold_bucket" {
  type        = string
  default     = "rail-performance-archiver-cold"
  description = "Prod cold bucket the rollup ships the DEEP_ARCHIVE tarball to (must match feeds.yaml s3.cold_bucket)."
}

# Schedule is created DISABLED so the first prod run is the manual, verified
# run-task in Phase D; flip to true (terraform apply) once that run checks out.
variable "rollup_schedule_enabled" {
  type        = bool
  default     = false
  description = "Whether the daily EventBridge schedule that runs the rollup is ENABLED."
}

variable "rollup_schedule_expression" {
  type        = string
  default     = "cron(30 3 * * ? *)"
  description = "EventBridge Scheduler expression (UTC) for the daily rollup — ~03:30Z per the design doc."
}

variable "hot_scratch_retention_days" {
  type    = number
  default = 14
}

variable "rollup_cpu" {
  type        = string
  default     = "4096" # 4 vCPU
  description = "Fargate task CPU units. Design targets up to 8192 (8 vCPU); start smaller and measure (the tail is a few big serial feeds, so more cores may not help)."
}

variable "rollup_memory" {
  type    = string
  default = "8192" # 8 GiB (valid with 4 vCPU)
}

variable "env_secret_name" {
  type        = string
  default     = "rail-archiver/env"
  description = "Secrets Manager secret holding the agency API keys as a JSON object (populated out-of-band, not by Terraform)."
}

variable "agency_secret_keys" {
  type = list(string)
  default = [
    "BAY_AREA_511_API_KEY",
    "MARTA_API_KEY",
    "METRA_API_KEY",
    "METRO_HOUSTON_API_KEY",
    "SAN_DIEGO_MTS_API_KEY",
    "SOUND_TRANSIT_API_KEY",
    "SWIFTLY_API_KEY",
    "TRIMET_API_KEY",
    "VALLEY_METRO_API_KEY",
    "WMATA_API_KEY",
  ]
  description = "Agency API-key env vars the rollup needs (it builds feed clients). Each is injected from a JSON key in the env secret."
}

variable "log_retention_days" {
  type    = number
  default = 14
}
