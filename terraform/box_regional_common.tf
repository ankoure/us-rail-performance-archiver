# --- Shared bootstrap base for regional poller boxes (EU/AU) --------------- #
# Mirrors box.tf's local.poller_user_data (docker install, compose plugin,
# directory setup, swap, local-landing prune timer) verbatim. box.tf itself
# is deliberately left untouched (see its header + the "leave us-east-1
# alone" decision), so this is a small, intentional duplication rather than a
# refactor of the live box's resource.
#
# Regional boxes append their own tail (box_eu.tf/box_au.tf) that fetches
# .env from Secrets Manager and starts docker compose automatically -- unlike
# box.tf, they don't wait for a human to paste .env, since there's no
# existing box on that continent to avoid dual-polling against during cutover.
locals {
  regional_poller_bootstrap_base = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    # Everything here runs as root under cloud-init. NOTE: do NOT reference
    # ssm-user -- the SSM agent creates it lazily on first session connect, so
    # it does not exist at boot (box.tf hit this the hard way first).
    dnf install -y docker git
    systemctl enable --now docker

    # The base AL2023 `docker` package ships the engine but NOT the compose v2
    # plugin, so `docker compose` just prints help. Install the arm64 plugin.
    mkdir -p /usr/libexec/docker/cli-plugins
    curl -sSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-aarch64 \
      -o /usr/libexec/docker/cli-plugins/docker-compose
    chmod +x /usr/libexec/docker/cli-plugins/docker-compose

    mkdir -p /opt/rail-archiver
    cd /opt/rail-archiver
    git clone https://github.com/ankoure/us-rail-performance-archiver.git .

    # Single shard here (SHARD_COUNT=1 in each box's own tail below), but
    # pre-create both dirs -- harmless if shard-1 is never started, and one
    # less thing to add later if a region is ever split into 2 shards.
    #
    # NOTE: poll_state lives under data/ (config/feeds.yaml's
    # writer.poll_state_dir: ./data/poll_state, and compose.prod.yml mounts
    # ./data/poll_state/shard-N) -- NOT bare poll_state/ at the repo root.
    # box.tf's original script (which this is otherwise a copy of) still has
    # the bare-path version: that's a real, pre-existing mismatch from the
    # data/ repo restructure that was never fixed there (see
    # project_repo_restructure memory -- "EC2 poll_state migration still
    # pending as manual deploy follow-up") -- it's silently masked on the
    # live us-east-1 box because a human already created the right directory
    # by hand once, out of band. These boxes get no such manual follow-up, so
    # getting the real path right here is load-bearing, not cosmetic:
    # without it Docker auto-creates the bind-mount source as root on first
    # `up`, and the container (uid 1000) can't write its heartbeat file into
    # a root-owned directory, and crash-loops on a PermissionError forever.
    mkdir -p data/poll_state/shard-0 data/poll_state/shard-1 archive
    chown -R 1000:1000 data archive

    # 4 GiB swap on root. Safety net for any memory spike in the poller; low
    # swappiness so it's not eager. dd, not fallocate (see README).
    dd if=/dev/zero of=/swapfile bs=1M count=4096
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
    echo "vm.swappiness=10" > /etc/sysctl.d/99-swappiness.conf
    sysctl --system

    # Local landing prune, daily -- see box.tf's identical timer for the full
    # "why this exists" note (the 2026-08-20 disk-fill outage). printf
    # line-by-line, NOT a nested heredoc: this script's own <<-EOT wrapper
    # strips leading indentation by an amount tied to ITS closing marker, so a
    # nested heredoc's closing line would land at an unpredictable column.
    printf '%s\n' \
      '[Unit]' \
      'Description=Prune local rail-archiver landing zone (deletes only S3-cold-confirmed day-partitions)' \
      'After=docker.service' \
      'Requires=docker.service' \
      '' \
      '[Service]' \
      'Type=oneshot' \
      'ExecStart=/usr/bin/docker exec rail-archiver-app-shard-0-1 python pipeline/prune.py --keep-days 3 -v' \
      > /etc/systemd/system/rail-archiver-prune.service

    printf '%s\n' \
      '[Unit]' \
      'Description=Daily rail-archiver local landing zone prune' \
      '' \
      '[Timer]' \
      'OnCalendar=daily' \
      'RandomizedDelaySec=15m' \
      'Persistent=true' \
      '' \
      '[Install]' \
      'WantedBy=timers.target' \
      > /etc/systemd/system/rail-archiver-prune.timer

    systemctl daemon-reload
    systemctl enable --now rail-archiver-prune.timer
  EOT
}
