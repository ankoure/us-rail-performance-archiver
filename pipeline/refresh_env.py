"""Push new/rotated agency keys from the local .env to the shared Secrets
Manager secret, then refresh .env on every poller box and restart its
pollers so the change actually takes effect.

Each box's user_data (terraform/box.tf, box_eu.tf, box_au.tf) fetches
rail-archiver/env into /opt/rail-archiver/.env ONCE, at first boot — nothing
re-fetches it afterward, so editing the secret alone does nothing to an
already-running box. This script is the "afterward" step: it's the same
fetch-and-filter-and-append logic user_data runs, replayed over SSM --
including the same continent scoping (box_eu.tf's poller_eu_env_tail): each
box's refresh reads ITS OWN currently-running CONTINENT setting and re-filters
to just that region's keys, so a rotation can never hand a box back keys its
--continent filter already dropped. A box with no CONTINENT set (us-east-1,
until it's switched over) still gets the full unfiltered blob, matching its
current unfiltered polling scope.

Usage:
  python pipeline/refresh_env.py NEW_AGENCY_API_KEY [MORE_KEY ...]
  python pipeline/refresh_env.py EXISTING_KEY --only eu   # rotate a value, one box
  python pipeline/refresh_env.py NEW_KEY --dry-run
"""

import sys
from pathlib import Path

# Make the repo root importable when run as `python pipeline/refresh_env.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import logging
import time

import boto3

from archiver.logger import logger  # noqa: E402

# Deliberately no load_dotenv() here, unlike other pipeline/ scripts: this
# runs as the OPERATOR orchestrating the fleet (needs ec2:DescribeInstances +
# ssm:SendCommand on whatever AWS profile/session they have active), not as
# the box (whose identity is the static AWS_ACCESS_KEY_ID/SECRET in .env,
# scoped only for S3 + secretsmanager). Loading .env into the process env
# would make boto3 silently authenticate as the box instead of the operator --
# env vars take priority over AWS_PROFILE. .env is still read directly, for
# the key VALUES being pushed (_read_local_env), just not into the process env.

SECRET_ID = "rail-archiver/env"
SECRET_REGION = "us-east-1"  # the secret always lives here regardless of box region

# Mirrors deploy.yml's matrix: which shard services actually need restarting
# on each box. Only the poller shards read agency keys -- datadog-agent and
# autoheal are left alone. name_tag matches box.tf/box_eu.tf/box_au.tf's
# `Name` tag -- NOT Application=rail-archiver, which also (surprisingly)
# tags i-0cc0bacc7e0bb6189, an unrelated gtfs-rt-rater/dashboard-api box.
BOXES = [
    {
        "name": "us",
        "region": "us-east-1",
        "name_tag": "rail-archiver-poller",
        "shard_services": "app-shard-0 app-shard-1",
    },
    {
        "name": "eu",
        "region": "eu-central-1",
        "name_tag": "rail-archiver-poller-eu",
        "shard_services": "app-shard-0",
    },
    {
        "name": "au",
        "region": "ap-southeast-2",
        "name_tag": "rail-archiver-poller-au",
        "shard_services": "app-shard-0",
    },
]


def _build_refresh_script(shard_services: str) -> str:
    # Built fresh per box (not a shared template + later .format()) so the
    # embedded python one-liner's OWN f-string braces (f"{k}={v}") don't
    # collide with a second templating pass over the same string.
    #
    # Reads the box's OWN currently-running CONTINENT out of its existing
    # .env (not something this script decides) -- requires scripts/
    # agency_env_keys.py to already be in the deployed image, i.e. this
    # refresh won't work correctly until that ships.
    return f"""
set -euo pipefail
CONTINENT=$(grep -oP '^CONTINENT=\\K.*' /opt/rail-archiver/.env || true)
IMAGE="ghcr.io/ankoure/us-rail-performance-archiver:latest"
if [ -n "$CONTINENT" ]; then
  docker run --rm "$IMAGE" python scripts/agency_env_keys.py --continent "$CONTINENT" > /tmp/agency_keys.txt
else
  docker run --rm "$IMAGE" python scripts/agency_env_keys.py > /tmp/agency_keys.txt
fi

aws secretsmanager get-secret-value --region {SECRET_REGION} --secret-id {SECRET_ID} \\
  --query SecretString --output text \\
  | python3 -c '
import json, sys
always = {{"DD_API_KEY", "DD_SITE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}}
with open("/tmp/agency_keys.txt") as f:
    wanted = always | {{line.strip() for line in f if line.strip()}}
d = json.load(sys.stdin)
print(chr(10).join(f"{{k}}={{v}}" for k, v in d.items() if k in wanted))
' > /tmp/rail-archiver-env-new
grep -E '^(CONTINENT|SHARD_COUNT|DD_HOSTNAME|WORKERS)=' /opt/rail-archiver/.env >> /tmp/rail-archiver-env-new || true
mv /tmp/rail-archiver-env-new /opt/rail-archiver/.env
chmod 600 /opt/rail-archiver/.env
rm -f /tmp/agency_keys.txt
cd /opt/rail-archiver
docker compose -f compose.prod.yml up -d --force-recreate {shard_services}
"""


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "keys",
        nargs="+",
        help="Env var name(s) to read from the local .env and push (new keys "
        "or rotated values of existing ones)",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=["us", "eu", "au"],
        help="Restrict the box refresh to these boxes (default: all three)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the merged secret and which boxes would be refreshed, without changing anything",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def _read_local_env(path: str = ".env") -> dict[str, str]:
    return dict(
        line.strip().split("=", 1)
        for line in open(path)
        if "=" in line and not line.startswith("#")
    )


def _merge_secret(keys: list[str], dry_run: bool) -> dict[str, str]:
    client = boto3.client("secretsmanager", region_name=SECRET_REGION)
    current = json.loads(client.get_secret_value(SecretId=SECRET_ID)["SecretString"])

    local_env = _read_local_env()
    missing = [k for k in keys if k not in local_env]
    if missing:
        raise SystemExit(f"Not found in local .env: {missing}")

    updated = dict(current)
    updated.update({k: local_env[k] for k in keys})

    changed = {k for k in keys if current.get(k) != updated[k]}
    if not changed:
        logger.info(
            "No values actually changed -- secret already up to date for %s", keys
        )
    else:
        logger.info("Updating: %s", sorted(changed))

    if dry_run:
        logger.info("[dry-run] would put-secret-value with %d total keys", len(updated))
    else:
        client.put_secret_value(SecretId=SECRET_ID, SecretString=json.dumps(updated))
        logger.info("Secret updated (%d total keys)", len(updated))
    return updated


def _find_instance_id(region: str, name_tag: str) -> str:
    client = boto3.client("ec2", region_name=region)
    resp = client.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [name_tag]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    instances = [i for r in resp["Reservations"] for i in r["Instances"]]
    if len(instances) != 1:
        raise SystemExit(
            f"Expected exactly 1 running '{name_tag}' instance in {region}, found {len(instances)}"
        )
    return instances[0]["InstanceId"]


def _refresh_box(box: dict, dry_run: bool) -> None:
    region, name = box["region"], box["name"]
    instance_id = _find_instance_id(region, box["name_tag"])
    logger.info("[%s] %s -> %s", name, region, instance_id)

    if dry_run:
        logger.info(
            "[dry-run] [%s] would refresh .env and restart: %s",
            name,
            box["shard_services"],
        )
        return

    ssm = boto3.client("ssm", region_name=region)
    script = _build_refresh_script(box["shard_services"])
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script]},
        Comment=f"refresh_env.py: sync .env from {SECRET_ID}",
    )["Command"]["CommandId"]

    # Poll rather than the boto3 waiter -- the waiter's fixed attempt budget
    # is tuned for a quick SSM round trip, not "docker pull + restart," and
    # timed out mid-command during manual testing of this exact refresh flow.
    for _ in range(30):
        time.sleep(2)
        invocation = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
        if invocation["Status"] not in ("Pending", "InProgress"):
            break
    else:
        raise SystemExit(f"[{name}] SSM command {command_id} did not finish in time")

    if invocation["Status"] != "Success":
        raise SystemExit(
            f"[{name}] SSM command failed ({invocation['Status']}): "
            f"{invocation['StandardErrorContent']}"
        )
    logger.info("[%s] refreshed and restarted (%s)", name, box["shard_services"])


def main(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    _merge_secret(args.keys, args.dry_run)

    boxes = [b for b in BOXES if not args.only or b["name"] in args.only]
    for box in boxes:
        _refresh_box(box, args.dry_run)


if __name__ == "__main__":
    main(parse_args())
