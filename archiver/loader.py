# loader.py
import os
import yaml
from archiver.archiver import FeedArchiver
from archiver.auth import APIClient
from archiver.config import (
    AgencyConfig,
    ArchiverConfig,
    APIKeyAuthConfig,
    BearerAuthConfig,
    BasicAuthConfig,
    NoAuthConfig,
)
from archiver.feed import Feed
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


def build_archiver(config: ArchiverConfig) -> FeedArchiver:
    feeds: list[Feed] = []
    for agency in config.agencies:
        client = build_client(agency)
        for feed_cfg in agency.feeds:
            feeds.append(
                Feed(
                    name=feed_cfg.name,
                    path=feed_cfg.path,
                    client=client,
                    expected_format=feed_cfg.expected_format,
                )
            )
    writer = LocalWriter(str(config.writer.base_dir))
    return FeedArchiver(feeds=feeds, writer=writer)
