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
    RateLimitConfig,
    S3Config,
    TelemetryConfig,
    WriterConfig,
)
from archiver.rate_limit import NullRateLimiter, RateLimiter, TokenBucket
from archiver.decoder import Decoder
from archiver.feed import Feed
from archiver.parser import Parser
from archiver.poll_state import PollStateStore
from archiver.rollup import Rollup
from archiver.shard import belongs_to_shard
from archiver.shipper import Shipper
from archiver.sink import LocalSink, S3Sink, TeeSink, Sink
from archiver.source import LocalSource, S3Source, Source
from archiver.telemetry import NoOpTelemetry, Telemetry
from archiver.uploader import Uploader
from archiver.writer import BaseWriter, BatchingWriter, LocalWriter


def _read_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"Required env var '{name}' is not set")
    return value


def load_config(path: str) -> ArchiverConfig:
    with open(path, "r") as f:
        return ArchiverConfig.model_validate(yaml.safe_load(f))


def build_limiter(cfg: RateLimitConfig | None) -> RateLimiter:
    if cfg is None:
        return NullRateLimiter()
    capacity = cfg.burst if cfg.burst is not None else cfg.requests
    return TokenBucket(capacity=capacity, refill_rate=cfg.requests / cfg.per_seconds)


def build_client(agency: AgencyConfig) -> APIClient:
    base_url = str(agency.base_url)
    limiter = build_limiter(agency.rate_limit)
    match agency.auth:
        case NoAuthConfig():
            return APIClient(base_url, limiter=limiter)
        case APIKeyAuthConfig() as a:
            if a.header is not None:
                return APIClient.with_api_key(
                    base_url, key=_read_env(a.env), header=a.header, limiter=limiter
                )
            else:
                return APIClient.with_api_key_query(
                    base_url, key=_read_env(a.env), param=a.param, limiter=limiter
                )
        case BearerAuthConfig() as a:
            return APIClient.with_bearer(
                base_url, token=_read_env(a.env), limiter=limiter
            )
        case BasicAuthConfig() as a:
            return APIClient.with_basic(
                base_url,
                username=_read_env(a.username_env),
                password=_read_env(a.password_env),
                limiter=limiter,
            )


def build_feeds(
    config: ArchiverConfig, shard_index: int = 0, shard_count: int = 1
) -> list[Feed]:
    feeds: list[Feed] = []
    for agency in config.agencies:
        if not belongs_to_shard(agency.agency_id, shard_index, shard_count):
            continue
        client = build_client(agency)
        for feed_cfg in agency.feeds:
            feeds.append(
                Feed(
                    name=feed_cfg.name,
                    path=feed_cfg.path,
                    client=client,
                    parser=Parser.from_name(feed_cfg.expected_format),
                    decoder=Decoder.from_name(feed_cfg.decoder),
                    poll_interval_seconds=feed_cfg.poll_interval_seconds,
                    agency_id=agency.agency_id,
                )
            )
    return feeds


def build_telemetry(config: TelemetryConfig, shard_index: int = 0) -> Telemetry:
    if not config.enabled:
        return NoOpTelemetry()

    # Lazy import — datadog only loaded if actually enabled
    from datadog.dogstatsd.base import DogStatsd
    from archiver.telemetry_datadog import DatadogTelemetry

    client = DogStatsd(host=config.agent_host, port=config.statsd_port)
    default_tags = {
        "service": config.service,
        "env": config.env,
        "shard": str(shard_index),
        **config.tags,
    }
    return DatadogTelemetry(client, default_tags=default_tags)


def build_sink(writer: WriterConfig, uploader: Uploader | None) -> Sink:
    match writer.landing_mode:
        case "local":
            return LocalSink(writer.landing_dir)
        case "dual":
            return TeeSink(
                [
                    LocalSink(writer.landing_dir),  # local FIRST
                    S3Sink(uploader, writer.landing_bucket, writer.landing_prefix),
                ]
            )
        case "s3":
            return S3Sink(uploader, writer.landing_bucket, writer.landing_prefix)
        case other:
            raise ValueError(f"Unsupported landing_mode: {other}")


def build_source(config: ArchiverConfig) -> Source:
    w = config.writer
    match w.rollup_source:
        case "local":
            return LocalSource(w.landing_dir)
        case "s3":
            telemetry = build_telemetry(config.telemetry)
            return S3Source(
                build_uploader(config.s3, telemetry), w.landing_bucket, w.landing_prefix
            )

        case other:
            raise ValueError(f"Unsupported rollup_source: {other}")


def build_writer(config: ArchiverConfig) -> BaseWriter:
    writer = config.writer
    match writer.writer_type:
        case "local":
            return LocalWriter(writer.landing_dir)  # legacy per-poll; no sink
        case "batch":
            uploader = None
            if writer.landing_mode in ("dual", "s3"):
                telemetry = build_telemetry(config.telemetry)
                uploader = build_uploader(config.s3, telemetry)
            sink = build_sink(writer, uploader)
            return BatchingWriter(writer.landing_dir, sink, writer.window_seconds)

        case other:
            raise ValueError(f"Unsupported writer_type: {other}")


def build_archiver(
    config: ArchiverConfig, shard_index: int, shard_count: int
) -> FeedArchiver:
    feeds = build_feeds(config, shard_index, shard_count)
    writer = build_writer(config)
    telemetry = build_telemetry(config.telemetry, shard_index)
    store = PollStateStore(str(config.writer.poll_state_dir))
    return FeedArchiver(feeds=feeds, writer=writer, telemetry=telemetry, store=store)


def build_rollup(config: ArchiverConfig) -> Rollup:
    feeds = build_feeds(config)
    telemetry = build_telemetry(config.telemetry)
    return Rollup(
        feeds=feeds,
        source=build_source(config),
        curated_dir=config.writer.curated_dir,
        telemetry=telemetry,
    )


def build_uploader(config: S3Config, telemetry: Telemetry) -> Uploader:
    if not config.enabled:
        raise RuntimeError("s3 is not enabled in config")

    # Lazy import — boto3 only loaded if actually enabled
    import boto3
    from botocore.config import Config
    from archiver.uploader import S3Uploader, InstrumentedUploader

    # Shipper.run fans uploads across a ThreadPoolExecutor (default 8 workers) that
    # share this one client; the default urllib3 pool of 10 then thrashes ("Connection
    # pool is full, discarding connection"). Size the pool comfortably above the ship
    # worker count (plus headroom for multipart) so connections are reused, not dropped.
    client = boto3.client(
        "s3",
        region_name=config.region,
        config=Config(max_pool_connections=25),
    )
    return InstrumentedUploader(S3Uploader(client), telemetry)


def build_shipper(config: ArchiverConfig) -> Shipper:
    telemetry = build_telemetry(config.telemetry)
    uploader = build_uploader(config.s3, telemetry)
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
