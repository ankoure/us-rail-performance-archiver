# --- Networking (default VPC) --------------------------------------------- #
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  # Only the default (public, IGW-routed) subnet per AZ — a Fargate task in a
  # private subnet with assignPublicIp can't actually reach S3/GHCR.
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# Egress-only SG: the task reaches S3 + GHCR over the internet gateway.
resource "aws_security_group" "rollup" {
  name = "rail-archiver-rollup"
  # NOTE: AWS SG description is immutable — do not edit this string, it forces
  # destroy+recreate of the SG for zero functional benefit. Now shared by the
  # rollup/gold/ship stages, despite the stale wording.
  description = "Egress-only for the Fargate rollup task"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- ECS cluster (Fargate Spot) ------------------------------------------- #
resource "aws_ecs_cluster" "main" {
  name = "rail-archiver"
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }
}

# Shared by all three stages — one place to look for the whole day's run.
resource "aws_cloudwatch_log_group" "rollup" {
  name              = "/ecs/rail-archiver-rollup"
  retention_in_days = var.log_retention_days
}

# --- Shared task script pieces ---------------------------------------------- #
# Split from a single rollup->gold->ship container into one ECS task def per
# stage (chained by a Step Functions state machine — see
# rollup_stepfunctions.tf) so a failed stage doesn't silently re-run the ones
# before it and each stage gets its own success/failure signal.
locals {
  # Overlay rollup_source=s3 + the prod hot bucket onto the baked feeds.yaml (so
  # there's no second config to keep in sync).
  config_overlay = "python -c 'import os, yaml; c = yaml.safe_load(open(\"config/feeds.yaml\")); c[\"writer\"][\"rollup_source\"] = \"s3\"; c[\"s3\"][\"hot_bucket\"] = os.environ[\"HOT_BUCKET\"]; c[\"telemetry\"][\"enabled\"] = True; c[\"telemetry\"][\"agent_host\"] = \"127.0.0.1\"; c[\"telemetry\"][\"env\"] = \"prod\"; yaml.safe_dump(c, open(\"/tmp/fargate.yaml\", \"w\"))'"

  # All three stages must agree on the same "day", even though they now run as
  # separate tasks possibly hours apart. RUN_DATE is a container-override env
  # var the state machine sets once (from the execution's start time) and
  # passes to every stage, so `date -u -d "$RUN_DATE -1 day"` can't drift
  # across a UTC-midnight boundary the way three independent
  # `date -u -d yesterday` calls could. A manual run-task (no override, e.g.
  # a one-off backfill) falls back to today's wall clock, matching the old
  # single-container behavior.
  day_prelude = <<-EOT
    set -e
    RUN_DATE="$${RUN_DATE:-$(date -u +%F)}"
    DAY="$${ROLLUP_DAY:-$(date -u -d "$RUN_DATE -1 day" +%F)}"
    echo "rollup day: $DAY"
    # awsvpc puts every container in one netns, so each stage reaches the
    # datadog-agent sidecar at 127.0.0.1:8125; env=prod matches the dashboard filter.
    ${local.config_overlay}
  EOT

  # Drain: DogStatsD is fire-and-forget UDP and the sidecar flushes on an
  # interval, so pause before the essential container exits (which SIGTERMs
  # the agent) to let this stage's final metrics reach Datadog.
  drain = "sleep 15"

  rollup_script = <<-EOT
    ${local.day_prelude}
    python rollup.py --config /tmp/fargate.yaml --day "$DAY"
    ${local.drain}
  EOT

  gold_script = <<-EOT
    ${local.day_prelude}
    python gold.py --config /tmp/fargate.yaml --day "$DAY"
    ${local.drain}
  EOT

  ship_script = <<-EOT
    ${local.day_prelude}
    python ship.py --config /tmp/fargate.yaml --day "$DAY"
    # Cert-expiry probe: emits cert.days_remaining per agency (drives the TLS
    # monitors). Independent of --day. MUST use /tmp/fargate.yaml so telemetry is
    # enabled — the baked config has it off, which would silently NoOp the metric.
    # `|| true`: an auxiliary check must never abort the drain (set -e) and cost
    # the run's ship.* metrics.
    python cert_check.py --config /tmp/fargate.yaml || true
    ${local.drain}
  EOT

  # Env every stage's app container needs (same S3 bucket + checksum workaround
  # rollup and ship both hit; kept identical across stages for parity).
  stage_environment = [
    { name = "HOT_BUCKET", value = var.hot_bucket },
    # botocore >=1.36 wraps the upload body in a non-seekable AwsChunkedWrapper
    # for its default request checksum; a retried PutObject then dies with
    # UnseekableStreamError. ship.py uses the same uploader, so disable the
    # default checksum here too (S3 does not require it).
    { name = "AWS_REQUEST_CHECKSUM_CALCULATION", value = "when_required" },
  ]

  stage_secrets = [
    for k in var.agency_secret_keys :
    { name = k, valueFrom = "${aws_secretsmanager_secret.env.arn}:${k}::" }
  ]

  # Identical sidecar in all three task defs — one place to edit its config.
  datadog_agent_container = {
    name      = "datadog-agent"
    image     = "gcr.io/datadoghq/agent:7" # same major as the box
    essential = false                      # task lifecycle follows the app container, not the agent
    memory    = 512                        # hard cap so the agent can't starve the stage's working set
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
        "awslogs-group"         = aws_cloudwatch_log_group.rollup.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "dd-agent"
      }
    }
  }
}

# --- Task definitions ------------------------------------------------------- #
# Each stage has its own cpu/memory vars (variables.tf) so they can be sized
# independently. gold/ship default to the same size as rollup for now —
# rollup.py is the one with OOM history, gold/ship are almost certainly
# lighter but haven't been measured running standalone yet.
resource "aws_ecs_task_definition" "rollup" {
  family                   = "rail-archiver-rollup"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.rollup_cpu
  memory                   = var.rollup_memory
  execution_role_arn       = aws_iam_role.rollup_execution.arn
  task_role_arn            = aws_iam_role.rollup_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # image is amd64 (multi-arch is a step-4 concern)
  }

  container_definitions = jsonencode([
    {
      name        = "rollup"
      image       = var.rollup_image
      essential   = true
      command     = ["sh", "-c", local.rollup_script]
      environment = local.stage_environment
      secrets     = local.stage_secrets
      # Start the sidecar before the app container so early metrics aren't
      # dropped onto a dead UDP socket (DogStatsD fails silently). START, not
      # HEALTHY — no healthcheck.
      dependsOn = [
        { containerName = "datadog-agent", condition = "START" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.rollup.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "rollup"
        }
      }
    },
    local.datadog_agent_container,
  ])
}

resource "aws_ecs_task_definition" "gold" {
  family                   = "rail-archiver-gold"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.gold_cpu
  memory                   = var.gold_memory
  execution_role_arn       = aws_iam_role.rollup_execution.arn
  task_role_arn            = aws_iam_role.rollup_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name        = "gold"
      image       = var.rollup_image
      essential   = true
      command     = ["sh", "-c", local.gold_script]
      environment = local.stage_environment
      secrets     = local.stage_secrets
      dependsOn = [
        { containerName = "datadog-agent", condition = "START" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.rollup.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "gold"
        }
      }
    },
    local.datadog_agent_container,
  ])
}

resource "aws_ecs_task_definition" "ship" {
  family                   = "rail-archiver-ship"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.ship_cpu
  memory                   = var.ship_memory
  execution_role_arn       = aws_iam_role.rollup_execution.arn
  task_role_arn            = aws_iam_role.rollup_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name        = "ship"
      image       = var.rollup_image
      essential   = true
      command     = ["sh", "-c", local.ship_script]
      environment = local.stage_environment
      secrets     = local.stage_secrets
      dependsOn = [
        { containerName = "datadog-agent", condition = "START" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.rollup.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ship"
        }
      }
    },
    local.datadog_agent_container,
  ])
}
