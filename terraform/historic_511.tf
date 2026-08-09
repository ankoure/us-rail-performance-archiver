# --- historic_511: monthly pull of 511.org's historic archive (RG operator) #
# Lands the raw monthly zip (GTFS static + stop_observations.txt) for the
# combined Bay Area "RG" operator in the hot bucket, under historic/511/rg/.
# Not a parse/replace step yet — see pipeline/historic_511.py's module
# docstring. Verified by hand against the live endpoint (200, application/zip,
# ~605 MiB for 2026-06) with the existing BAY_AREA_511_API_KEY before writing
# this task definition.

resource "aws_cloudwatch_log_group" "historic_511" {
  name              = "/ecs/rail-archiver-historic-511"
  retention_in_days = var.log_retention_days
}

# Minimal task role: only the hot bucket, same shape as gold_backfill_task's
# policy in gold_backfill.tf. GetObject authorizes the exists() idempotency
# gate (there is no real s3:HeadObject IAM action), PutObject ships the zip,
# ListBucket lets HeadObject on a genuinely-missing key 404 instead of 403
# (see gold_backfill.tf's ListHotForExists comment for why that matters).
resource "aws_iam_role" "historic_511_task" {
  name               = "rail-archiver-historic-511-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "historic_511_task" {
  name = "historic-511-hot-bucket"
  role = aws_iam_role.historic_511_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadWriteHot"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["arn:aws:s3:::${var.hot_bucket}/*"]
      },
      {
        Sid      = "ListHotForExists"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = ["arn:aws:s3:::${var.hot_bucket}"]
      },
    ]
  })
}

locals {
  historic_511_script = <<-EOT
    set -e
    # Same telemetry overlay the other split-out tasks use — see cert_check.tf's
    # comment for why agent_host must be 127.0.0.1 on Fargate.
    python -c 'import yaml; c = yaml.safe_load(open("config/feeds.yaml")); c["telemetry"]["enabled"] = True; c["telemetry"]["agent_host"] = "127.0.0.1"; c["telemetry"]["env"] = "prod"; yaml.safe_dump(c, open("/tmp/fargate.yaml", "w"))'
    START=$(date +%s)
    trap 'python pipeline/task_duration.py --config /tmp/fargate.yaml --metric pipeline.historic_511.duration --seconds $(( $(date +%s) - START )) || true' EXIT
    # MONTH is optional -- historic_511.py defaults to the previous UTC month
    # when --month is omitted. Set it via an environment override (not a
    # command override, which would skip the yaml overlay above) to backfill
    # an arbitrary past month, e.g.:
    #   --overrides '{"containerOverrides":[{"name":"historic-511","environment":[{"name":"MONTH","value":"2026-02"}]}]}'
    if [ -n "$${MONTH:-}" ]; then
      python pipeline/historic_511.py --config /tmp/fargate.yaml --month "$MONTH"
    else
      python pipeline/historic_511.py --config /tmp/fargate.yaml
    fi
    # Drain: let the run's final historic.511.* metric reach the sidecar before SIGTERM.
    sleep 15
  EOT
}

resource "aws_ecs_task_definition" "historic_511" {
  family                   = "rail-archiver-historic-511"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # I/O-bound (stream a zip to disk, then upload it), not compute-heavy —
  # unlike rollup/gold_backfill this doesn't reuse their cpu/memory.
  cpu                = "256"
  memory             = "1024"
  execution_role_arn = aws_iam_role.rollup_execution.arn
  task_role_arn      = aws_iam_role.historic_511_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # image is amd64, same as the rollup task
  }

  container_definitions = jsonencode([
    {
      name      = "historic-511"
      image     = var.rollup_image
      essential = true
      command   = ["sh", "-c", local.historic_511_script]
      secrets = [
        { name = "BAY_AREA_511_API_KEY", valueFrom = "${aws_secretsmanager_secret.env.arn}:BAY_AREA_511_API_KEY::" }
      ]
      dependsOn = [
        { containerName = "datadog-agent", condition = "START" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.historic_511.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "historic-511"
        }
      }
    },
    {
      name      = "datadog-agent"
      image     = "gcr.io/datadoghq/agent:7"
      essential = false
      memory    = 512
      environment = [
        { name = "DD_SITE", value = "datadoghq.com" },
        { name = "DD_DOGSTATSD_NON_LOCAL_TRAFFIC", value = "true" },
        { name = "DD_APM_ENABLED", value = "false" },
        { name = "ECS_FARGATE", value = "true" },
      ]
      secrets = [
        { name = "DD_API_KEY", valueFrom = "${aws_secretsmanager_secret.env.arn}:DD_API_KEY::" }
      ]
      stopTimeout = 120
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.historic_511.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "dd-agent"
        }
      }
    }
  ])
}
