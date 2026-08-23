# EC2 Deployment Setup

One-time setup for the `deploy.yml` GitHub Action. The flow is:

```
push to main → CI builds image → pushes to ghcr.io → assumes AWS role via OIDC
              → SSM send-command → instance pulls image → docker compose up -d
```

No SSH key, no port 22, no static AWS credentials in GitHub.

You only do this once. After that, every push to `main` deploys automatically.

---

## What you'll need

- An AWS account with admin (or enough to create IAM + EC2)
- The GitHub repo: `ankoure/us-rail-performance-archiver`
- Your `.env` file (the one you use locally — secrets for Datadog, AWS, and feed API keys)

---

## Step 1 — Make the GHCR image accessible to the instance

The first push of `deploy.yml` creates the package `ghcr.io/ankoure/us-rail-performance-archiver`. By default it's private.

Easiest path: **make it public**. On GitHub → your profile → Packages → the package → Package settings → Change visibility → Public. The EC2 instance can then `docker pull` with no auth.

If you'd rather keep it private, create a GHCR read-only PAT and `docker login ghcr.io -u <user> -p <pat>` on the instance once during step 4 — credentials persist in `~/.docker/config.json`.

---

## Step 2 — AWS IAM: trust GitHub via OIDC

### 2a. Create the OIDC identity provider

Once per AWS account.

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

(The thumbprint is a sentinel; AWS now verifies via its own CA bundle and ignores this field, but the CLI still requires it.)

### 2b. Create the deploy role

The role the GitHub workflow assumes. It can only:
- send a single SSM RunCommand to your specific instance
- read SSM command status

Save as `deploy-role-trust.json` — fill in your AWS account ID:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:ankoure/us-rail-performance-archiver:ref:refs/heads/main"
      }
    }
  }]
}
```

Save as `deploy-role-policy.json` — fill in ACCOUNT_ID and each box's REGION/INSTANCE_ID
after step 3 (one `ec2:instance` ARN per box now that `deploy.yml` fans out to all
3 regions, plus one `ssm:document` ARN per region — that ARN type is
region-scoped, not account-scoped, so it doesn't collide across regions):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ec2:us-east-1:ACCOUNT_ID:instance/US_INSTANCE_ID",
        "arn:aws:ec2:eu-central-1:ACCOUNT_ID:instance/EU_INSTANCE_ID",
        "arn:aws:ec2:ap-southeast-2:ACCOUNT_ID:instance/AU_INSTANCE_ID",
        "arn:aws:ssm:us-east-1::document/AWS-RunShellScript",
        "arn:aws:ssm:eu-central-1::document/AWS-RunShellScript",
        "arn:aws:ssm:ap-southeast-2::document/AWS-RunShellScript"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
      "Resource": "*"
    }
  ]
}
```

Create the role:

```bash
aws iam create-role \
  --role-name rail-archiver-deploy \
  --assume-role-policy-document file://deploy-role-trust.json

aws iam put-role-policy \
  --role-name rail-archiver-deploy \
  --policy-name deploy-ssm \
  --policy-document file://deploy-role-policy.json
```

Note the **role ARN** — you'll need it for `AWS_DEPLOY_ROLE_ARN` in step 5.

---

## Step 3 — EC2 instance

> **Note:** the sections below describe the *original* manual bootstrap (single
> box, hand-launched, local EBS landing volume). The live us-east-1 box has
> since moved to Terraform-managed provisioning (`terraform/box.tf`) with S3
> landing and no data volume, and two more region-local boxes now exist —
> `terraform/box_eu.tf` (Frankfurt) and `terraform/box_au.tf` (Sydney), each
> polling only their own continent's agencies via `--continent`. This section
> is kept for the IAM role/policy JSON (still accurate and reused as-is
> across all three boxes) and the general shape of the SSM bootstrap — for an
> actual new box, apply the relevant `terraform/box*.tf` file instead of
> hand-launching, then follow 3c's `.env` step below.

### 3a. IAM role for the instance

The instance needs SSM agent comms + S3 access (for `pipeline/ship.py`).

```bash
aws iam create-role \
  --role-name rail-archiver-instance \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }'

aws iam attach-role-policy \
  --role-name rail-archiver-instance \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam create-instance-profile --instance-profile-name rail-archiver-instance
aws iam add-role-to-instance-profile \
  --instance-profile-name rail-archiver-instance \
  --role-name rail-archiver-instance
```

Then attach an inline S3 policy that allows writes to your archive buckets — adjust bucket names to whatever you use:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:HeadObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::your-cold-bucket",
      "arn:aws:s3:::your-cold-bucket/*",
      "arn:aws:s3:::your-hot-bucket",
      "arn:aws:s3:::your-hot-bucket/*"
    ]
  }]
}
```

### 3b. Launch the instance

- **AMI:** Amazon Linux 2023 (SSM agent preinstalled)
- **Instance type:** start with `t3.small` (2 vCPU, 2 GiB). Bump to `t3.medium` if the batch rollup OOMs
- **Storage:** root = 30 GiB gp3; **plus** a separate EBS volume of 500 GiB gp3 for the landing zone (it grows fast — ~222 GB / 36 feeds in May 2026 before shipping to deep archive trims it)
- **Security group:** outbound all, inbound none (SSM doesn't need open ports)
- **IAM instance profile:** `rail-archiver-instance` (from 3a)

Note the **instance ID** — you'll need it for `EC2_INSTANCE_ID` in step 5, and to fill in `deploy-role-policy.json` above.

### 3c. Bootstrap the instance

Open a session via SSM (no SSH key needed):

```bash
aws ssm start-session --target i-XXXXXXXX
```

Then on the instance:

```bash
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ssm-user

# Mount the 500 GiB EBS volume at /opt/rail-archiver/archive.
# Find the device name with `lsblk` — usually /dev/nvme1n1.
sudo mkfs.xfs /dev/nvme1n1
sudo mkdir -p /opt/rail-archiver
sudo mount /dev/nvme1n1 /opt/rail-archiver
echo "/dev/nvme1n1 /opt/rail-archiver xfs defaults,nofail 0 2" | sudo tee -a /etc/fstab

# Add swap. The box ships with none, and the once-daily batch rollup spikes memory
# on the big feeds (e.g. NYCT/BART). Without swap, an overrun on 2026-06-01 became an
# unrecoverable page-cache refault storm (~18h wedge) instead of a clean OOM-kill.
# 4 GiB on the data volume (plenty free), low swappiness so it's a safety net, not
# eager paging. Use dd (not fallocate) — XFS rejects swapon on a holey file.
sudo dd if=/dev/zero of=/opt/rail-archiver/swapfile bs=1M count=4096 status=progress
sudo chmod 600 /opt/rail-archiver/swapfile
sudo mkswap /opt/rail-archiver/swapfile
sudo swapon /opt/rail-archiver/swapfile
echo "/opt/rail-archiver/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab
echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl --system
# Verify: `swapon --show` and `free -h` should now show 4 GiB of swap.

# Clone the repo.
sudo chown ssm-user:ssm-user /opt/rail-archiver
cd /opt/rail-archiver
git clone https://github.com/ankoure/us-rail-performance-archiver.git .

# Create .env from your local one (paste contents).
nano .env

# On the EU/AU boxes ONLY, also add (see compose.prod.yml for how these are
# consumed -- unset on the us-east-1 box's .env, which keeps its behavior
# unchanged: no --continent filter, 2 shards, default Datadog hostname):
#   CONTINENT=eu        # or au
#   SHARD_COUNT=1        # single shard is enough at EU/AU's current agency counts
#   DD_HOSTNAME=rail-archiver-eu   # or rail-archiver-au -- avoids colliding with the us box in Datadog

# If the GHCR image is private, log in once:
# docker login ghcr.io -u ankoure
```

Then do a manual first run to confirm everything wires up:

```bash
cd /opt/rail-archiver

# The poller runs sharded (app-shard-0/1 in compose.prod.yml), and each shard mounts
# its OWN ./data/poll_state/shard-<i> so the per-container HEALTHCHECK heartbeat stays
# independent. Docker would create those bind-mount dirs as root, but the containers
# run as 1000:1000 — pre-create them owned correctly (make target does the mkdir):
make shard-dirs
sudo chown -R 1000:1000 data/poll_state/   # skip if your deploy user is already uid 1000

docker compose -f compose.prod.yml pull   # pulls the image CI pushed
docker compose -f compose.prod.yml up -d --remove-orphans   # --remove-orphans drops renamed/old services (e.g. the pre-shard `app`)
docker compose -f compose.prod.yml logs -f app-shard-0   # (and app-shard-1)
```

On the EU/AU boxes, name only the services they actually run (single shard,
no `app-shard-1` -- matches what `deploy.yml`'s matrix passes on every deploy):

```bash
docker compose -f compose.prod.yml pull datadog-agent app-shard-0 autoheal
docker compose -f compose.prod.yml up -d --remove-orphans datadog-agent app-shard-0 autoheal
```

If logs show feed polls landing in `./archive/` and both shards report `healthy`
(`docker compose -f compose.prod.yml ps`), you're done with the bootstrap.

---

## Step 4 — GitHub repo settings

### 4a. Secrets

Settings → Secrets and variables → Actions → **New repository secret**:

| Name                  | Value                                                                |
|-----------------------|----------------------------------------------------------------------|
| `AWS_DEPLOY_ROLE_ARN` | ARN of `rail-archiver-deploy` from step 2b                           |
| `EC2_INSTANCE_IDS`    | JSON object of all 3 boxes' instance IDs (see below)                 |

`EC2_INSTANCE_IDS` replaces the old single `EC2_INSTANCE_ID` secret now that
`deploy.yml` fans out to 3 regional boxes via a matrix — GitHub Actions can't
index the `secrets` context by a matrix value, so one JSON blob decoded with
`jq` stands in for 3 separate secrets:

```json
{"us": "i-XXXXXXXX", "eu": "i-YYYYYYYY", "au": "i-ZZZZZZZZ"}
```

Pull the IDs from `terraform output poller_eu_instance_id` /
`poller_au_instance_id` (and the existing us-east-1 instance ID) after applying
`terraform/box_eu.tf`/`box_au.tf`.

`deploy-role-policy.json`'s `ssm:SendCommand` resource ARN above also needs an
entry per region/instance now (the `arn:aws:ec2:REGION:...` line), since each
is a distinct ARN.

### 4b. Regions

`deploy.yml`'s matrix (`strategy.matrix.include`) pins each box to its region
directly (`us-east-1`/`eu-central-1`/`ap-southeast-2`) — edit that list if a
box ever moves.

### 4c. Workflow permissions

Default GitHub Actions permissions are usually fine. If your org tightened them, ensure: Settings → Actions → General → Workflow permissions → "Read and write" (or at least: contents: read, packages: write, id-token: write — `deploy.yml` requests these explicitly).

---

## Step 5 — Trigger a deploy

Either:
- Push any commit to `main`, or
- Actions tab → "deploy" → Run workflow

The first build will be slow (no cache); subsequent builds reuse the GHA cache layer. Watch the `deploy` job's stdout/stderr groups for SSM output from the instance.

---

## Troubleshooting

**"InvalidInstanceId" in SSM:** instance isn't registered. Check that the IAM instance profile has `AmazonSSMManagedInstanceCore`, then reboot. Confirm with `aws ssm describe-instance-information --filters "Key=InstanceIds,Values=i-XXX"`.

**"AccessDenied" assuming the role:** the OIDC `sub` claim in the trust policy must match exactly. For deploys from non-main branches or PRs, broaden the `StringLike` (e.g., `repo:ankoure/us-rail-performance-archiver:*`).

**`docker pull` fails with auth error on instance:** image is still private and the box has no GHCR credentials. Either make the package public (recommended) or `docker login ghcr.io` on the box.

**`git reset --hard` blows away local edits:** by design. The instance should never be the source of truth — edit locally, push, redeploy.

**Need to roll back:** `git checkout <previous-sha>` on the box, then `IMAGE_TAG=<short-sha> docker compose -f compose.prod.yml up -d`. CI tags every build with the 7-char SHA, so any past build is pullable.
