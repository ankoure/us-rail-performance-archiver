from pathlib import Path
from typing import Literal, Annotated, Protocol
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


class HasName(Protocol):
    name: str


def names_must_be_unique(
    config_list: list[HasName], config_model_name: str
) -> list[HasName]:
    names = [config.name for config in config_list]
    if len(names) != len(set(names)):
        duplicates = {name for name in names if names.count(name) > 1}
        raise ValueError(
            f"{config_model_name} names must be unique, duplicates found: {duplicates}"
        )
    return config_list


class NoAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["none"]


class APIKeyAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["api_key"]
    header: str | None = None
    param: str | None = None
    env: str

    @model_validator(mode="after")
    def exactly_one_of_header_or_param(self) -> "APIKeyAuthConfig":
        if self.header is None and self.param is None:
            raise ValueError("Either header or param should be set")
        if self.header and self.param:
            raise ValueError("Header and param are both set")
        return self


class BearerAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["bearer"]
    env: str


class BasicAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["basic"]
    username_env: str
    password_env: str


AuthConfig = Annotated[
    NoAuthConfig | APIKeyAuthConfig | BearerAuthConfig | BasicAuthConfig,
    Field(discriminator="type"),
]


class FeedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    path: str
    expected_format: Literal["protobuf", "json", "auto"] = "protobuf"
    decoder: Literal["standard", "mta_nyct", "marta_json"] = "standard"


class AgencyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agency_id: str
    name: str
    region: str
    base_url: HttpUrl
    auth: AuthConfig
    feeds: list[FeedConfig]

    @field_validator("feeds")
    @classmethod
    def validate_feeds(cls, v: list[FeedConfig]) -> list[FeedConfig]:
        return names_must_be_unique(v, "feed")


class WriterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_dir: Path


class TelemetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    service: str = "rail-archiver"
    env: str = "dev"
    agent_host: str = "localhost"
    statsd_port: int = 8125
    tags: dict[str, str] = {}


class ArchiverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    writer: WriterConfig
    telemetry: TelemetryConfig = TelemetryConfig()
    agencies: list[AgencyConfig]

    @field_validator("agencies")
    @classmethod
    def validate_agencies(cls, v: list[AgencyConfig]) -> list[AgencyConfig]:
        return names_must_be_unique(v, "agency")

    @model_validator(mode="after")
    def feed_names_globally_unique(self) -> "ArchiverConfig":
        all_feed_names = [
            feed.name for agency in self.agencies for feed in agency.feeds
        ]
        if len(all_feed_names) != len(set(all_feed_names)):
            duplicates = {n for n in all_feed_names if all_feed_names.count(n) > 1}
            raise ValueError(
                f"feed names must be globally unique, duplicates: {duplicates}"
            )
        return self
