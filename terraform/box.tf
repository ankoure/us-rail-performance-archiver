# --- Downsized poller box (Phase E) --------------------------------------- #
# Replaces the hand-built t3.large (x86, 500 GiB) with a Terraform-managed
# t4g.small (ARM, 30 GiB root only). The rollup moved to Fargate and the landing
# moved to S3, so the box is now poller-only: ~0.1 core of I/O-bound polling plus
# the in-process LandingUploader. No data volume — local landing is just the
# transient outbox the uploader drains and deletes.
#
# user_data bootstraps everything EXCEPT `.env` (pasted out-of-band via an SSM
# session) and the `docker compose up` (held until .env exists, to avoid the new
# box dual-polling the live one during cutover). A marker file signals readiness.

variable "poller_instance_type" {
  type    = string
  default = "t4g.small" # ARM/Graviton, 2 vCPU / 2 GiB
}

variable "poller_root_gb" {
  type    = number
  default = 30
}

variable "poller_subnet_id" {
  type        = string
  default     = "subnet-0fc4958b243ac7bdc" # default-VPC public subnet, us-east-1a
  description = "Public subnet (MapPublicIpOnLaunch) so the poller reaches feeds/S3/GHCR."
}

variable "poller_security_group_id" {
  type        = string
  default     = "sg-01d9a0cd549f0c197" # the existing box's egress-only SG (reused)
  description = "Egress-only SG (no inbound; SSM needs none)."
}

# Always the latest AL2023 arm64 AMI (SSM-agent preinstalled).
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  poller_user_data = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    dnf install -y docker git
    systemctl enable --now docker
    usermod -aG docker ssm-user

    # 4 GiB swap on root (no data volume now). Safety net for any memory spike in
    # the poller; low swappiness so it's not eager. dd, not fallocate (see README).
    dd if=/dev/zero of=/swapfile bs=1M count=4096
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
    echo "vm.swappiness=10" > /etc/sysctl.d/99-swappiness.conf
    sysctl --system

    mkdir -p /opt/rail-archiver
    chown ssm-user:ssm-user /opt/rail-archiver
    cd /opt/rail-archiver
    sudo -u ssm-user git clone https://github.com/ankoure/us-rail-performance-archiver.git .
    sudo -u ssm-user make shard-dirs
    chown -R 1000:1000 poll_state/

    # Bootstrap done. Pollers are NOT started here: that waits until .env is
    # added (SSM session) and the operator runs `docker compose up`, so the new
    # box doesn't dual-poll the old one mid-cutover.
    touch /opt/rail-archiver/.bootstrap-complete
  EOT
}

resource "aws_instance" "poller" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.poller_instance_type
  subnet_id                   = var.poller_subnet_id
  vpc_security_group_ids      = [var.poller_security_group_id]
  iam_instance_profile        = var.instance_role_name # rail-archiver-instance
  associate_public_ip_address = true
  user_data                   = local.poller_user_data

  root_block_device {
    volume_size = var.poller_root_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  tags = {
    Name = "rail-archiver-poller"
  }
}

output "poller_instance_id" {
  value = aws_instance.poller.id
}

output "poller_public_ip" {
  value = aws_instance.poller.public_ip
}
