#!/usr/bin/env bash
# Manual/backfill ECS RunTask wrapper.
#
# Tags every task it starts trigger=manual, so it's distinguishable in Cost
# Explorer / terraform/cost_monitoring.tf from EventBridge Scheduler runs
# (tagged trigger=scheduled in terraform/aux_schedule.tf and
# terraform/rollup_schedule.tf). Without this, a deliberate backfill and a
# runaway job look identical in cost data.
#
# Cluster/subnets/security-group are read from `terraform output`, so they
# never drift from what's actually deployed.
#
# Usage:
#   scripts/run_task.sh <task-family> [--spot] [--profile PROFILE]
#
# Examples:
#   scripts/run_task.sh rail-archiver-rollup --profile KourePowerUser
#   scripts/run_task.sh rail-archiver-historic-511-otp --spot --profile KourePowerUser
#
# Task families (see terraform/*.tf): rail-archiver-rollup,
# rail-archiver-cert-check, rail-archiver-s3-storage-metrics,
# rail-archiver-historic-511, rail-archiver-historic-511-otp,
# rail-archiver-gold-backfill.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <task-family> [--spot] [--profile PROFILE]" >&2
  exit 1
fi

task_family="$1"
shift

spot=false
profile_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --spot)
      spot=true
      shift
      ;;
    --profile)
      profile_args=(--profile "$2")
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tf_dir="$script_dir/../terraform"

cluster=$(terraform -chdir="$tf_dir" output -raw rollup_cluster)
subnets=$(terraform -chdir="$tf_dir" output -json rollup_subnet_ids | jq -r 'join(",")')
security_group=$(terraform -chdir="$tf_dir" output -raw rollup_security_group)

launch_args=(--launch-type FARGATE)
if $spot; then
  # aws ecs run-task rejects --launch-type FARGATE_SPOT outright; Spot is only
  # selectable via --capacity-provider-strategy (reference_ecs_spot_run_task
  # memory — hit this twice writing 511 backfill commands by hand).
  launch_args=(--capacity-provider-strategy capacityProvider=FARGATE_SPOT,weight=1)
fi

aws ecs run-task \
  "${profile_args[@]}" \
  --cluster "$cluster" \
  --task-definition "$task_family" \
  --count 1 \
  "${launch_args[@]}" \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$security_group],assignPublicIp=ENABLED}" \
  --tags key=trigger,value=manual
