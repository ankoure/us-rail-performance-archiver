# --- AU/NZ (Sydney) poller box ---------------------------------------------- #
# Region-local companion to box.tf's live us-east-1 box (see
# project_regional_poller_expansion); see box_eu.tf's header for the shared
# rationale (S3 landing + Fargate rollup stay centralized, only the poller is
# regional, and this box fully bootstraps itself from Secrets Manager rather
# than waiting on a human-pasted .env). Scoped to AU-tagged agencies via
# `--continent au`. Currently only ~4 agencies -- comfortably one shard on the
# smallest box in the family; t4g.small is likely overkill here and a good
# first candidate to right-size down to t4g.micro once it's been observed
# running for a while.

variable "poller_au_instance_type" {
  type        = string
  default     = "t4g.small" # see box_eu.tf's instance-type note; AU's ~4 agencies need even less than EU's ~37
  description = "Instance type for the AU/Sydney poller box."
}

variable "poller_au_root_gb" {
  type        = number
  default     = 30 # see box_eu.tf's root-size note -- same reasoning applies
  description = "Root EBS volume size (GiB) for the AU/Sydney poller box."
}

# Default VPC in ap-southeast-2 -- see box_eu.tf's networking note.
data "aws_vpc" "au_default" {
  provider = aws.au
  default  = true
}

data "aws_subnets" "au_default" {
  provider = aws.au
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.au_default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_security_group" "poller_au" {
  provider    = aws.au
  name        = "rail-archiver-poller-au"
  description = "Egress-only for the AU poller box (no inbound; SSM needs none)"
  vpc_id      = data.aws_vpc.au_default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Always the latest AL2023 arm64 AMI in ap-southeast-2 (SSM-agent preinstalled).
data "aws_ssm_parameter" "al2023_arm64_au" {
  provider = aws.au
  name     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# See box_eu.tf's poller_eu_env_tail for the full rationale (secret piping,
# set -x safety, unverified-AL2023-awscli caveat) -- identical except for the
# continent/hostname.
locals {
  poller_au_env_tail = <<-EOT
    command -v aws >/dev/null 2>&1 || dnf install -y awscli2 || dnf install -y aws-cli

    cd /opt/rail-archiver
    docker compose -f compose.prod.yml pull datadog-agent app-shard-0 autoheal

    # Trim the shared secret down to only what --continent au will ever read --
    # see box_eu.tf's poller_eu_env_tail for the full rationale.
    docker run --rm ghcr.io/ankoure/us-rail-performance-archiver:latest \
      python scripts/agency_env_keys.py --continent au > /tmp/agency_keys.txt

    aws secretsmanager get-secret-value \
      --region ${var.region} \
      --secret-id ${var.env_secret_name} \
      --query SecretString --output text \
      | python3 -c '
    import json, sys
    always = set(${jsonencode(local.poller_always_keep_keys)})
    with open("/tmp/agency_keys.txt") as f:
        wanted = always | {line.strip() for line in f if line.strip()}
    d = json.load(sys.stdin)
    print("\n".join(f"{k}={v}" for k, v in d.items() if k in wanted))
    ' > /opt/rail-archiver/.env
    rm -f /tmp/agency_keys.txt
    chmod 600 /opt/rail-archiver/.env

    printf '%s\n' \
      'CONTINENT=au' \
      'SHARD_COUNT=1' \
      'DD_HOSTNAME=rail-archiver-au' \
      >> /opt/rail-archiver/.env

    docker compose -f compose.prod.yml up -d datadog-agent app-shard-0 autoheal

    touch /opt/rail-archiver/.bootstrap-complete
  EOT
}

resource "aws_instance" "poller_au" {
  provider = aws.au

  ami                         = data.aws_ssm_parameter.al2023_arm64_au.value
  instance_type               = var.poller_au_instance_type
  subnet_id                   = data.aws_subnets.au_default.ids[0] # single box, no HA need -- any AZ works
  vpc_security_group_ids      = [aws_security_group.poller_au.id]
  iam_instance_profile        = var.instance_role_name # rail-archiver-instance -- IAM is account-global, reused as-is across regions
  associate_public_ip_address = true
  user_data                   = "${local.regional_poller_bootstrap_base}\n${local.poller_au_env_tail}"

  root_block_device {
    volume_size = var.poller_au_root_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  lifecycle {
    ignore_changes = [ami] # see box.tf's aws_instance.poller for why
  }

  tags = {
    Name = "rail-archiver-poller-au"
    # Required: deploy.yml's SSM SendCommand is tag-scoped to Application=rail-archiver.
    Application = "rail-archiver"
  }
}

output "poller_au_instance_id" {
  value = aws_instance.poller_au.id
}

output "poller_au_public_ip" {
  value = aws_instance.poller_au.public_ip
}
