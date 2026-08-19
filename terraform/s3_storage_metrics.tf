# --- s3_storage_metrics: S3 storage-cost dashboard widgets, split out of -- #
# --- the rollup task ------------------------------------------------------- #
# Reads CloudWatch's free daily BucketSizeBytes metric and emits s3.storage.bytes
# gauges. Doesn't touch curated_dir and doesn't take --day, so like cert_check
# (see cert_check.tf) it can run on its own schedule instead of riding along
# inside the rollup. CloudWatch only publishes BucketSizeBytes once/day, so
# running this more than once/day would just re-read the same datapoint.
#
# Also runs a live hot/cold LIST scan (pipeline/s3_agency_scan.py, shared with
# scripts/s3_cost_report.py) to emit per-agency s3.storage.cost_estimated_usd
# gauges — CloudWatch's bucket-level metric can't be broken down by agency.

resource "aws_cloudwatch_log_group" "s3_storage_metrics" {
  name              = "/ecs/rail-archiver-s3-storage-metrics"
  retention_in_days = var.log_retention_days
}

# Minimal task role: only the CloudWatch reads this script needs. Split out of
# rollup_task's policy (which used to carry this Sid for the same reason) so
# this task isn't also holding the rollup task's S3 write access to prod
# hot/cold that it never uses.
resource "aws_iam_role" "s3_storage_metrics_task" {
  name               = "rail-archiver-s3-storage-metrics-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "s3_storage_metrics_task" {
  name = "read-bucket-size-metrics"
  role = aws_iam_role.s3_storage_metrics_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadBucketSizeMetrics"
        Effect = "Allow"
        # ListMetrics discovers which StorageType series a bucket actually
        # publishes (no "AllStorageTypes" catch-all exists); GetMetricStatistics
        # reads each one. CloudWatch metric reads aren't resource-scoped, hence "*".
        Action   = ["cloudwatch:ListMetrics", "cloudwatch:GetMetricStatistics"]
        Resource = ["*"]
      },
      {
        # Read-only: the per-agency scan only lists object keys/sizes
        # (list_objects_v2), never reads object contents, so no GetObject.
        Sid      = "ListHotColdForAgencyScan"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = ["arn:aws:s3:::${var.hot_bucket}", "arn:aws:s3:::${var.cold_bucket}"]
      },
    ]
  })
}

locals {
  s3_storage_metrics_script = <<-EOT
    set -e
    # Same telemetry overlay cert_check.tf uses — see its comment for why
    # agent_host must be overridden to 127.0.0.1 on Fargate.
    python -c 'import yaml; c = yaml.safe_load(open("config/feeds.yaml")); c["telemetry"]["enabled"] = True; c["telemetry"]["agent_host"] = "127.0.0.1"; c["telemetry"]["env"] = "prod"; yaml.safe_dump(c, open("/tmp/fargate.yaml", "w"))'
    START=$(date +%s)
    trap 'python pipeline/task_duration.py --config /tmp/fargate.yaml --metric pipeline.s3_storage_metrics.duration --seconds $(( $(date +%s) - START )) || true' EXIT
    python pipeline/s3_storage_metrics.py --config /tmp/fargate.yaml
    # Drain: DogStatsD is fire-and-forget UDP and the sidecar flushes on an
    # interval, so pause before the essential container exits (which SIGTERMs
    # the agent) to let the run's metrics reach Datadog.
    sleep 15
  EOT
}

resource "aws_ecs_task_definition" "s3_storage_metrics" {
  family                   = "rail-archiver-s3-storage-metrics"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.rollup_execution.arn
  task_role_arn            = aws_iam_role.s3_storage_metrics_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # image is amd64, same as the rollup task
  }

  container_definitions = jsonencode([
    {
      name      = "s3-storage-metrics"
      image     = var.rollup_image
      essential = true
      command   = ["sh", "-c", local.s3_storage_metrics_script]
      dependsOn = [
        { containerName = "datadog-agent", condition = "START" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.s3_storage_metrics.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "s3-storage-metrics"
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
          "awslogs-group"         = aws_cloudwatch_log_group.s3_storage_metrics.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "dd-agent"
        }
      }
    }
  ])
}
