# --- cert_check: SSL cert-expiry probe, split out of the rollup task ------ #
# Emits cert.days_remaining per agency (drives the TLS monitors). Independent
# of --day and never touches curated_dir, so unlike rollup/gtfs/gold/ship (see
# the NOTE above locals.rollup_script in rollup.tf) it doesn't need to share
# disk with anything else — it can run on its own schedule instead of riding
# along inside the ~2.5h rollup window.
#
# No task_role_arn: this script makes no AWS API calls of its own (just a TLS
# handshake per agency + dogstatsd over UDP to the sidecar).

resource "aws_cloudwatch_log_group" "cert_check" {
  name              = "/ecs/rail-archiver-cert-check"
  retention_in_days = var.log_retention_days
}

locals {
  cert_check_script = <<-EOT
    set -e
    # Same telemetry overlay the rollup task uses, minus the rollup_source /
    # hot_bucket bits this script never touches. MUST override agent_host: the
    # baked config points at the Docker-Compose service name "datadog-agent",
    # which doesn't resolve on Fargate's shared-netns awsvpc (127.0.0.1 does).
    python -c 'import yaml; c = yaml.safe_load(open("config/feeds.yaml")); c["telemetry"]["enabled"] = True; c["telemetry"]["agent_host"] = "127.0.0.1"; c["telemetry"]["env"] = "prod"; yaml.safe_dump(c, open("/tmp/fargate.yaml", "w"))'
    START=$(date +%s)
    trap 'python pipeline/task_duration.py --config /tmp/fargate.yaml --metric pipeline.cert_check.duration --seconds $(( $(date +%s) - START )) || true' EXIT
    python pipeline/cert_check.py --config /tmp/fargate.yaml
    # Drain: DogStatsD is fire-and-forget UDP and the sidecar flushes on an
    # interval, so pause before the essential container exits (which SIGTERMs
    # the agent) to let the run's metrics reach Datadog.
    sleep 15
  EOT
}

resource "aws_ecs_task_definition" "cert_check" {
  family                   = "rail-archiver-cert-check"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.rollup_execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # image is amd64, same as the rollup task
  }

  container_definitions = jsonencode([
    {
      name      = "cert-check"
      image     = var.rollup_image
      essential = true
      command   = ["sh", "-c", local.cert_check_script]
      # Start the sidecar before the probe so early metrics aren't dropped onto
      # a dead UDP socket (DogStatsD fails silently). START, not HEALTHY.
      dependsOn = [
        { containerName = "datadog-agent", condition = "START" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.cert_check.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "cert-check"
        }
      }
    },
    {
      name      = "datadog-agent"
      image     = "gcr.io/datadoghq/agent:7" # same major as the rollup task's sidecar
      essential = false                      # task lifecycle follows `cert-check`, not the agent
      memory    = 512                        # hard cap so the agent can't starve the task's working set
      environment = [
        { name = "DD_SITE", value = "datadoghq.com" },
        { name = "DD_DOGSTATSD_NON_LOCAL_TRAFFIC", value = "true" },
        { name = "DD_APM_ENABLED", value = "false" }, # we only use dogstatsd
        { name = "ECS_FARGATE", value = "true" },
      ]
      secrets = [
        { name = "DD_API_KEY", valueFrom = "${aws_secretsmanager_secret.env.arn}:DD_API_KEY::" }
      ]
      stopTimeout = 120 # Fargate max — lets SIGTERM trigger a final flush
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.cert_check.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "dd-agent"
        }
      }
    }
  ])
}
