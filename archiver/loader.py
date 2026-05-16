# loader.py
import os
import yaml
import archiver.decoder  # noqa: F401 — populate Decoder._registry via import side effects
import archiver.parser  # noqa: F401 — populate Parser._registry via import side effects
from archiver.archiver import FeedArchiver
from archiver.auth import APIClient
from archiver.config import (
    AgencyConfig,
    ArchiverConfig,
    APIKeyAuthConfig,
    BearerAuthConfig,
    BasicAuthConfig,
    NoAuthConfig,
    S3Config,
    TelemetryConfig,
)
from archiver.decoder import Decoder
from archiver.feed import Feed
from archiver.parser import Parser
from archiver.rollup import Rollup
from archiver.shipper import Shipper
from archiver.telemetry import NoOpTelemetry, Telemetry
from archiver.uploader import Uploader
from archiver.writer import LocalWriter


def _read_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"Required env var '{name}' is not set")
    return value


def load_config(path: str) -> ArchiverConfig:
    with open(path, "r") as f:
        return ArchiverConfig.model_validate(yaml.safe_load(f))


def build_client(agency: AgencyConfig) -> APIClient:
    base_url = str(agency.base_url)
    match agency.auth:
        case NoAuthConfig():
            return APIClient(base_url)
        case APIKeyAuthConfig() as a:
            if a.header is not None:
                return APIClient.with_api_key(
                    base_url, key=_read_env(a.env), header=a.header
                )
            else:
                return APIClient.with_api_key_query(
                    base_url, key=_read_env(a.env), param=a.param
                )
        case BearerAuthConfig() as a:
            return APIClient.with_bearer(base_url, token=_read_env(a.env))
        case BasicAuthConfig() as a:
            return APIClient.with_basic(
                base_url,
                username=_read_env(a.username_env),
                password=_read_env(a.password_env),
            )


def build_feeds(config: ArchiverConfig) -> list[Feed]:
    feeds: list[Feed] = []
    for agency in config.agencies:
        client = build_client(agency)
        for feed_cfg in agency.feeds:
            feeds.append(
                Feed(
                    name=feed_cfg.name,
                    path=feed_cfg.path,
                    client=client,
                    parser=Parser.from_name(feed_cfg.expected_format),
                    decoder=Decoder.from_name(feed_cfg.decoder),
                )
            )
    return feeds


def build_telemetry(config: TelemetryConfig) -> Telemetry:
    if not config.enabled:
        return NoOpTelemetry()

    # Lazy import — datadog only loaded if actually enabled
    from datadog.dogstatsd.base import DogStatsd
    from archiver.telemetry_datadog import DatadogTelemetry

    client = DogStatsd(host=config.agent_host, port=config.statsd_port)
    default_tags = {
        "service": config.service,
        "env": config.env,
        **config.tags,
    }
    return DatadogTelemetry(client, default_tags=default_tags)


def build_archiver(config: ArchiverConfig) -> FeedArchiver:
    feeds = build_feeds(config)
    writer = LocalWriter(str(config.writer.landing_dir))
    telemetry = build_telemetry(config.telemetry)
    return FeedArchiver(feeds=feeds, writer=writer, telemetry=telemetry)


def build_rollup(config: ArchiverConfig) -> Rollup:
    feeds = build_feeds(config)
    telemetry = build_telemetry(config.telemetry)
    return Rollup(
        feeds=feeds,
        landing_dir=config.writer.landing_dir,
        curated_dir=config.writer.curated_dir,
        telemetry=telemetry,
    )


def build_uploader(config: S3Config) -> Uploader:
    if not config.enabled:
        raise RuntimeError("s3 is not enabled in config")

    # Lazy import — boto3 only loaded if actually enabled
    import boto3
    from archiver.uploader import S3Uploader

    client = boto3.client("s3", region_name=config.region)
    return S3Uploader(client)


def build_shipper(config: ArchiverConfig) -> Shipper:
    uploader = build_uploader(config.s3)
    telemetry = build_telemetry(config.telemetry)
    return Shipper(
        landing_dir=config.writer.landing_dir,
        curated_dir=config.writer.curated_dir,
        uploader=uploader,
        cold_bucket=config.s3.cold_bucket,
        hot_bucket=config.s3.hot_bucket,
        cold_prefix=config.s3.cold_prefix,
        hot_prefix=config.s3.hot_prefix,
        telemetry=telemetry,
    )
