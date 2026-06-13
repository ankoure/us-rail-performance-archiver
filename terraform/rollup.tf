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
  name        = "rail-archiver-rollup"
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

resource "aws_cloudwatch_log_group" "rollup" {
  name              = "/ecs/rail-archiver-rollup"
  retention_in_days = var.log_retention_days
}

# --- Task command --------------------------------------------------------- #
# Overlay rollup_source=s3 + the prod hot bucket onto the baked feeds.yaml (so
# there's no second config to keep in sync), then rollup + ship (hot parquet AND
# the cold DEEP_ARCHIVE tarball — cold_bucket is already prod in feeds.yaml).
# ROLLUP_DAY can be set per run (override) to target a specific past day;
# defaults to yesterday-UTC. No prune: the landing.tf 7-day lifecycle expires S3
# landing, and the box no longer holds the curated tree.
locals {
  rollup_script = <<-EOT
    set -e
    DAY="$${ROLLUP_DAY:-$(date -u -d yesterday +%F)}"
    echo "rollup day: $DAY"
    python -c 'import os, yaml; c = yaml.safe_load(open("config/feeds.yaml")); c["writer"]["rollup_source"] = "s3"; c["s3"]["hot_bucket"] = os.environ["HOT_BUCKET"]; c["telemetry"]["enabled"] = False; yaml.safe_dump(c, open("/tmp/fargate.yaml", "w"))'
    python rollup.py --config /tmp/fargate.yaml --day "$DAY"
    python ship.py --config /tmp/fargate.yaml --day "$DAY"
  EOT
}

# --- Task definition ------------------------------------------------------ #
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
      name      = "rollup"
      image     = var.rollup_image
      essential = true
      command   = ["sh", "-c", local.rollup_script]
      environment = [
        { name = "HOT_BUCKET", value = var.hot_bucket },
      ]
      secrets = [
        for k in var.agency_secret_keys :
        { name = k, valueFrom = "${aws_secretsmanager_secret.env.arn}:${k}::" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.rollup.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "rollup"
        }
      }
    }
  ])
}
