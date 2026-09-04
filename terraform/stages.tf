# --- Per-stage rollup tasks (phase 1 of the stage split) ------------------- #
#
# The nightly batch used to be ONE task at rollup_cpu/rollup_memory running every
# stage, which meant every stage was provisioned at the max of all of them:
# rollup is CPU-bound (Rust decode), gtfs/gold/snapshot are memory-bound, and the
# ships are I/O-bound. Sizing them together made headroom untargetable -- giving
# gold more memory meant giving rollup the same.
#
# These three stages read nothing that any other stage writes (gtfs pulls static
# GTFS off the network, snapshot reads landing, cold-ship reads landing), so they
# split out with no shared-storage problem at all -- unlike gold, which reads
# rollup's silver parquet and is deferred to phase 2. That is what makes this
# safe where the 2026-07-31 Step Functions split was not: see the NOTE in
# rollup.tf, where gold saw an empty curated/ and silently no-op'd every feed.
#
# All three exclude local.heavy_agencies for the same reason the main task does:
# those agencies run their whole chain alone in rollup_heavy, and running their
# gtfs/snapshot here too would both duplicate the work and put GO_AHEAD's gtfs
# step -- the one that SIGKILLs at 8 GiB by itself -- back into a shared envelope.
#
# Sizes below are deliberately generous first guesses, not measured values. Watch
# pipeline.<stage>.duration and the task memory graphs for a week before cutting
# any of them (same discipline as rollup_memory's history).

locals {
  stage_defs = {
    gtfs = {
      cpu       = var.stage_gtfs_cpu
      memory    = var.stage_gtfs_memory
      stages    = "gtfs"
      workers   = var.stage_workers
      post      = ""
      silver    = ""
      scheduled = true
    }
    snapshot = {
      cpu       = var.stage_snapshot_cpu
      memory    = var.stage_snapshot_memory
      stages    = "snapshot"
      workers   = var.stage_workers
      post      = ""
      silver    = ""
      scheduled = true
    }
    # cold-ship + prune. Splitting the archive out means the raw DEEP_ARCHIVE
    # tarball is no longer downstream of ANY other stage -- stronger than the
    # 2026-09-03 archive-first reorder, which only moved it to the front of a
    # chain that could still die before reaching it.
    #
    # prune_s3 runs here and ONLY here: it sweeps the whole landing bucket, so a
    # second task doing it concurrently would be pure duplicated listing. It is
    # safe to run while other stages are still working -- it deletes only days
    # whose cold tarball is already confirmed in S3, and keep-days holds back the
    # recent days everything else is actually reading.
    archive = {
      cpu       = var.stage_archive_cpu
      memory    = var.stage_archive_memory
      stages    = "cold-ship"
      workers   = var.stage_workers
      silver    = ""
      scheduled = true
      post      = "python pipeline/prune_s3.py --config /tmp/fargate.yaml --keep-days ${var.landing_prune_keep_days} || true"
    }
    # Phase 2. Unlike the three above, gold READS another stage's output, so it
    # must run after rollup -- that ordering is the whole reason the state
    # machine in stage_orchestration.tf exists, and why this stage's schedule is
    # driven by the state machine rather than its own cron.
    #
    # It reads silver from the hot bucket (var.hot_bucket) rather than local
    # disk. That is not a new layout: Shipper._hot_key is the curated-relative
    # path, so the hot bucket already IS the curated tree, and pyarrow reads it
    # with row-group streaming (analysis/curated_fs.py). Marts are still written
    # to local ephemeral disk and uploaded by the implied hot-ship.
    gold = {
      cpu     = var.stage_gold_cpu
      memory  = var.stage_gold_memory
      stages  = "gold"
      workers = var.stage_workers
      post    = ""
      silver  = "--silver-dir s3://${var.hot_bucket}"
      # Sequenced by the state machine after rollup, not by its own cron.
      scheduled = false
    }
  }

  # Same shell shape as local.rollup_script -- config overlay, duration trap,
  # set +e around agency_batch so one agency's failure still surfaces in the
  # task's exit code without skipping the Datadog drain.
  stage_scripts = {
    for name, def in local.stage_defs : name => <<-EOT
      set -e
      DAY="$${ROLLUP_DAY:-$(date -u -d yesterday +%F)}"
      echo "stage ${name} day: $DAY"
      python -c 'import os, yaml; c = yaml.safe_load(open("config/feeds.yaml")); c["writer"]["rollup_source"] = "s3"; c["s3"]["hot_bucket"] = os.environ["HOT_BUCKET"]; c["telemetry"]["enabled"] = True; c["telemetry"]["agent_host"] = "127.0.0.1"; c["telemetry"]["env"] = "prod"; yaml.safe_dump(c, open("/tmp/fargate.yaml", "w"))'
      START=$(date +%s)
      trap 'python pipeline/task_duration.py --config /tmp/fargate.yaml --metric pipeline.stage_${name}.duration --seconds $(( $(date +%s) - START )) || true' EXIT

      set +e
      python pipeline/agency_batch.py --config /tmp/fargate.yaml --day "$DAY" --workers ${def.workers} --stages ${def.stages} ${def.silver} --exclude-agency ${join(" ", local.heavy_agencies)}
      AGENCY_STATUS=$?
      set -e
      if [ "$AGENCY_STATUS" -ne 0 ]; then
        echo "agency_batch (${name}): one or more agencies failed for $DAY -- see per-agency log lines above" >&2
      fi
      ${def.post}
      sleep 15
      exit "$AGENCY_STATUS"
    EOT
  }
}

resource "aws_cloudwatch_log_group" "stage" {
  for_each          = local.stage_defs
  name              = "/ecs/rail-archiver-stage-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "stage" {
  for_each                 = local.stage_defs
  family                   = "rail-archiver-stage-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  # Reuses the rollup roles: identical S3 + secrets access, and the landing
  # DeleteObject grant the archive stage's prune needs is already on them.
  execution_role_arn = aws_iam_role.rollup_execution.arn
  task_role_arn      = aws_iam_role.rollup_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "stage-${each.key}"
      image     = var.rollup_image
      essential = true
      command   = ["sh", "-c", local.stage_scripts[each.key]]
      environment = [
        { name = "HOT_BUCKET", value = var.hot_bucket },
        { name = "AWS_REQUEST_CHECKSUM_CALCULATION", value = "when_required" },
      ]
      dependsOn = [
        { containerName = "datadog-agent", condition = "START" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.stage[each.key].name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "stage-${each.key}"
        }
      }
    },
    {
      name        = "datadog-agent"
      image       = "gcr.io/datadoghq/agent:7"
      essential   = false
      memory      = 512
      stopTimeout = 120
      environment = [
        { name = "DD_SITE", value = "datadoghq.com" },
        { name = "DD_DOGSTATSD_NON_LOCAL_TRAFFIC", value = "true" },
        { name = "DD_APM_ENABLED", value = "false" },
        { name = "ECS_FARGATE", value = "true" },
      ]
      secrets = [
        { name = "DD_API_KEY", valueFrom = "${aws_secretsmanager_secret.env.arn}:DD_API_KEY::" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.stage[each.key].name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "dd-agent"
        }
      }
    },
  ])
}

# Phase 1 keeps plain schedules: these three stages have no dependency on each
# other or on rollup, so there is nothing to order. The Step Functions state
# machine arrives in phase 2, when gold has to be sequenced after rollup.
resource "aws_scheduler_schedule" "stage" {
  for_each = { for k, v in local.stage_defs : k => v if v.scheduled }
  name     = "rail-archiver-stage-${each.key}-daily"
  state    = var.stage_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.stage_schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.rollup_scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.stage[each.key].arn
      task_count          = 1
      tags                = { trigger = "scheduled" }
      # On-demand FARGATE, not Spot: same reasoning as the main rollup and
      # rollup_heavy -- these process a day's data and must complete. The
      # archive stage especially: a Spot reclaim mid-run means landing days
      # that never reach cold.
      launch_type = "FARGATE"

      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.rollup.id]
        assign_public_ip = true
      }
    }
  }
}
