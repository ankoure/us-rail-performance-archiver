# --- EU/Frankfurt poller box ------------------------------------------------ #
# Region-local companion to box.tf's live us-east-1 box (see
# project_regional_poller_expansion): only the poller is regional here, S3
# landing + the Fargate rollup both stay centralized in us-east-1. This box
# is scoped to EU-tagged agencies via `--continent eu` (set below, baked into
# user_data) -- at ~37 agencies that's comfortably one shard, well under what
# the us-east-1 box's two shards handle for ~144.
#
# Unlike box.tf, this box fully bootstraps itself: user_data fetches .env
# from Secrets Manager (rollup_secrets.tf's aws_secretsmanager_secret.env,
# read access granted in landing.tf) and starts docker compose automatically
# -- no human pasting .env via SSM, since there's no existing EU box to avoid
# dual-polling against during a cutover.

variable "poller_eu_instance_type" {
  type        = string
  default     = "t4g.small" # ARM/Graviton, 2 vCPU / 2 GiB -- same family as box.tf; one shard here needs far less than the US box's two-shard load, right-size down later if it's overkill
  description = "Instance type for the EU poller box."
}

variable "poller_eu_root_gb" {
  type = number
  # box.tf's ORIGINAL 30 GiB default, not its manually-bumped 50 -- that bump
  # was recovery from a local-landing-fills-disk outage on a box whose prune
  # timer was added by hand after the fact. This box ships with the prune
  # timer from first boot, so it shouldn't need the same emergency headroom.
  default     = 30
  description = "Root EBS volume size (GiB) for the EU poller box."
}

# Default VPC in eu-central-1. box.tf's us-east-1 box points at a literal
# subnet/SG ID because it was hand-built before Terraform existed for it;
# this is a fresh region with nothing to reference, so look it up the same
# way rollup.tf does for the Fargate task's networking.
data "aws_vpc" "eu_default" {
  provider = aws.eu
  default  = true
}

data "aws_subnets" "eu_default" {
  provider = aws.eu
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.eu_default.id]
  }
  # Only the default (public, IGW-routed) subnet per AZ -- this box needs a
  # public IP to reach GHCR/S3/feed hosts, same reasoning as rollup.tf.
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# Egress-only: no inbound, SSM needs none. Same shape as the us-east-1 box's
# reused sg-01d9a0cd549f0c197, but this region has no existing SG to reuse.
resource "aws_security_group" "poller_eu" {
  provider    = aws.eu
  name        = "rail-archiver-poller-eu"
  description = "Egress-only for the EU poller box (no inbound; SSM needs none)"
  vpc_id      = data.aws_vpc.eu_default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Always the latest AL2023 arm64 AMI in eu-central-1 (SSM-agent preinstalled).
data "aws_ssm_parameter" "al2023_arm64_eu" {
  provider = aws.eu
  name     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# Appended to local.regional_poller_bootstrap_base. Fetches the shared env
# secret and writes it straight to .env, then appends this box's own
# (non-secret) config. The secret VALUE only ever flows through the pipe
# into python's stdin -- never as a command-line argument -- so `set -x`'s
# command tracing (cloud-init logs every command) doesn't leak it; the
# CONTINENT/SHARD_COUNT/DD_HOSTNAME lines below aren't secret, so they're
# fine to trace. AWS CLI auth is implicit via the instance role (IMDSv2) --
# no credentials to manage here.
#
# NOTE: assumes AL2023 ships (or `dnf install`s cleanly as) a working `aws`
# CLI -- unverified against a live instance as of writing, same as the rest
# of this bootstrap script; watch /var/log/cloud-init-output.log on first
# apply, the same way box.tf's original gotchas were found empirically.
locals {
  poller_eu_env_tail = <<-EOT
    command -v aws >/dev/null 2>&1 || dnf install -y awscli2 || dnf install -y aws-cli

    aws secretsmanager get-secret-value \
      --region ${var.region} \
      --secret-id ${var.env_secret_name} \
      --query SecretString --output text \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print("\n".join(f"{k}={v}" for k,v in d.items()))' \
      > /opt/rail-archiver/.env
    chmod 600 /opt/rail-archiver/.env

    printf '%s\n' \
      'CONTINENT=eu' \
      'SHARD_COUNT=1' \
      'DD_HOSTNAME=rail-archiver-eu' \
      >> /opt/rail-archiver/.env

    cd /opt/rail-archiver
    docker compose -f compose.prod.yml pull datadog-agent app-shard-0 autoheal
    docker compose -f compose.prod.yml up -d datadog-agent app-shard-0 autoheal

    touch /opt/rail-archiver/.bootstrap-complete
  EOT
}

resource "aws_instance" "poller_eu" {
  provider = aws.eu

  ami           = data.aws_ssm_parameter.al2023_arm64_eu.value
  instance_type = var.poller_eu_instance_type
  # A single small box has no HA need across AZs -- any default-for-az
  # subnet in the list works, so just take the first.
  subnet_id                   = data.aws_subnets.eu_default.ids[0]
  vpc_security_group_ids      = [aws_security_group.poller_eu.id]
  iam_instance_profile        = var.instance_role_name # rail-archiver-instance -- IAM is account-global, reused as-is across regions
  associate_public_ip_address = true
  user_data                   = "${local.regional_poller_bootstrap_base}\n${local.poller_eu_env_tail}"

  root_block_device {
    volume_size = var.poller_eu_root_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  lifecycle {
    # Same rationale as box.tf's aws_instance.poller: the SSM AMI parameter
    # tracks "latest", and without this every plan after a new AL2023 AMI
    # ships would want to replace this LIVE box.
    ignore_changes = [ami]
  }

  tags = {
    Name = "rail-archiver-poller-eu"
    # Required: deploy.yml's SSM SendCommand is tag-scoped to Application=rail-archiver.
    Application = "rail-archiver"
  }
}

output "poller_eu_instance_id" {
  value = aws_instance.poller_eu.id
}

output "poller_eu_public_ip" {
  value = aws_instance.poller_eu.public_ip
}
