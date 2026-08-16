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
  type = number
  # Dropped 30->7 on 2026-07-07: prune_s3.py now handles proactive cleanup of
  # shipped partitions, so the lifecycle is just a backstop for anything missed.
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

# Re-enabled 2026-06-14: Shipper is now S3-aware (ships hot+cold from the S3
# landing, verified on 6/11+6/12) and the schedule runs on-demand FARGATE (not
# Spot). The end-to-end Fargate rollup+ship pipeline works, so the daily job is live.
variable "rollup_schedule_enabled" {
  type        = bool
  default     = true
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
  description = "Fargate task CPU units for the rollup stage. Design targets up to 8192 (8 vCPU); start smaller and measure (the tail is a few big serial feeds, so more cores may not help)."
}

variable "rollup_memory" {
  type = string
  # 10 GiB (was dropped to 8 GiB when this reverted to one combined task,
  # against a pre-dedup measured peak of 4.59 GiB — see git history). Back to
  # 10 GiB: the 2026-08-03 rollup run hit 7.8 GiB (90% of the 8 GiB ceiling)
  # ~3.5 min in and triggered a BrokenProcessPool OOM kill across 4 concurrent
  # ROLLUP_WORKERS, the first run after Deduper started holding a Python-level
  # _seen set of dedup-key tuples per worker on top of the Arrow batch memory.
  #
  # 2026-08-16: bumped 10 -> 20 GiB as a stopgap. 3 consecutive nightly runs
  # (days Aug 13-15) hit exitCode 137 / "OutOfMemoryError: container killed
  # due to memory usage" -- a task-level OOM, not a single-worker crash -- and
  # none of them reached the ship step, so nothing shipped to hot for 3 days.
  # The first of the 3 died on the *old* 255-feed config, before the Aug 14
  # global-feed merge, so this predates and is likely broader than the feed
  # count increase. Root cause still open -- NOT the dedup set above, which
  # was removed in 0dfa987 the same day as the incident that comment
  # describes (2026-08-03), so it hasn't existed for the 13 days this bug's
  # been live. This bump just buys headroom while the real cause is found.
  default     = "20480"
  description = "Fargate task memory (MiB) for the rollup stage."
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

# --- Aux tasks split out of the rollup task -------------------------------- #
# cert_check and s3_storage_metrics used to run as extra steps inside the
# rollup container (see the removed lines in rollup.tf's locals.rollup_script).
# Neither touches curated_dir or takes --day, so they don't need the rollup's
# shared local disk and can run on their own schedule instead. Same cautious
# rollout as rollup_schedule_enabled: start disabled, verify with a manual
# run-task, then flip to true.

variable "cert_check_schedule_enabled" {
  type        = bool
  default     = false
  description = "Whether the daily EventBridge schedule for the standalone cert_check task is ENABLED."
}

variable "cert_check_schedule_expression" {
  type        = string
  default     = "cron(0 5 * * ? *)"
  description = "EventBridge Scheduler expression (UTC) for the daily cert-expiry probe. No longer tied to the rollup window since it shares nothing with it."
}

variable "s3_storage_metrics_schedule_enabled" {
  type        = bool
  default     = false
  description = "Whether the daily EventBridge schedule for the standalone s3_storage_metrics task is ENABLED."
}

variable "s3_storage_metrics_schedule_expression" {
  type        = string
  default     = "cron(15 5 * * ? *)"
  description = "EventBridge Scheduler expression (UTC) for the daily S3 storage-cost metrics. CloudWatch's BucketSizeBytes only updates once/day, so once is enough; offset a few minutes from cert_check just to keep their log streams from interleaving."
}

variable "historic_511_schedule_enabled" {
  type        = bool
  default     = false
  description = "Whether the monthly EventBridge schedule for the historic_511 task is ENABLED. Same cautious rollout as the other schedule vars: apply disabled, verify with a manual run-task, then flip to true."
}

variable "historic_511_schedule_expression" {
  type        = string
  default     = "cron(0 6 5 * ? *)"
  description = "EventBridge Scheduler expression (UTC) for the monthly 511.org historic-archive pull — 5th of the month, giving 511 a few days' buffer to publish the prior month's archive."
}
