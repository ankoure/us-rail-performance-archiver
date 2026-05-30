from pydantic import ValidationError
import pytest

from unittest.mock import patch, mock_open

from archiver.loader import load_config

mock_yaml_content = """
writer:
  landing_dir: ./archive
  curated_dir: ./curated

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
    timezone: America/Los_Angeles
    base_url: https://api.bart.gov/gtfsrt
    auth:
      type: none
    feeds:
      - name: bart-trips
        path: /tripupdate.aspx
        expected_format: protobuf
        decoder: standard
      - name: bart-alerts
        path: /alerts.aspx
        expected_format: protobuf
        decoder: standard
"""

mock_yaml_content_extra_fields = """
writer:
  landing_dir: ./archive
  curated_dir: ./curated

telemetry:
  enbaled: true   # the typo we're catching

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
    timezone: America/Los_Angeles
    base_url: https://api.bart.gov/gtfsrt
    auth:
      type: none
    feeds:
      - name: bart-trips
        path: /tripupdate.aspx
        expected_format: protobuf
        decoder: standard
      - name: bart-alerts
        path: /alerts.aspx
        expected_format: protobuf
        decoder: standard
"""

mock_yaml_content_with_telemetry = """
writer:
  landing_dir: ./archive
  curated_dir: ./curated

telemetry:
  enabled: true
  service: my-test-service
  env: ci
  tags:
    region: us-west

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
    timezone: America/Los_Angeles
    base_url: https://api.bart.gov/gtfsrt
    auth:
      type: none
    feeds:
      - name: bart-trips
        path: /tripupdate.aspx
        expected_format: protobuf
        decoder: standard
      - name: bart-alerts
        path: /alerts.aspx
        expected_format: protobuf
        decoder: standard


"""


mock_yaml_content_poll_interval_is_zero = """
writer:
  landing_dir: ./archive
  curated_dir: ./curated

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
    timezone: America/Los_Angeles
    base_url: https://api.bart.gov/gtfsrt
    auth:
      type: none
    feeds:
      - name: bart-trips
        path: /tripupdate.aspx
        expected_format: protobuf
        decoder: standard
        poll_interval_seconds: 0
      - name: bart-alerts
        path: /alerts.aspx
        expected_format: protobuf
        decoder: standard
        poll_interval_seconds: 0
"""

mock_yaml_content_poll_interval_is_neg_1 = """
writer:
  landing_dir: ./archive
  curated_dir: ./curated

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
    timezone: America/Los_Angeles
    base_url: https://api.bart.gov/gtfsrt
    auth:
      type: none
    feeds:
      - name: bart-trips
        path: /tripupdate.aspx
        expected_format: protobuf
        decoder: standard
        poll_interval_seconds: -1
      - name: bart-alerts
        path: /alerts.aspx
        expected_format: protobuf
        decoder: standard
        poll_interval_seconds: -1
"""

mock_yaml_content_poll_interval_is_30 = """
writer:
  landing_dir: ./archive
  curated_dir: ./curated

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
    timezone: America/Los_Angeles
    base_url: https://api.bart.gov/gtfsrt
    auth:
      type: none
    feeds:
      - name: bart-trips
        path: /tripupdate.aspx
        expected_format: protobuf
        decoder: standard
        poll_interval_seconds: 30
      - name: bart-alerts
        path: /alerts.aspx
        expected_format: protobuf
        decoder: standard
        poll_interval_seconds: 30
"""


def _mock_load_config(mock_yaml_content: str):
    with patch("builtins.open", mock_open(read_data=mock_yaml_content)):
        with open("fake_config.yaml", "r") as f:
            config = load_config(f)
            return config


def test_archiverconfig_parses_with_no_telemetry_key():
    config = _mock_load_config(mock_yaml_content)
    assert config.telemetry.enabled is False


def test_telemetry_typo_rejected():
    with pytest.raises(ValidationError):
        _mock_load_config(mock_yaml_content_extra_fields)


def test_explicit_telemetry_values_pass_through():
    config = _mock_load_config(mock_yaml_content_with_telemetry)
    assert config.telemetry.enabled is True
    assert config.telemetry.service == "my-test-service"
    assert config.telemetry.env == "ci"
    assert config.telemetry.tags == {"region": "us-west"}


def test_poll_interval_omitted_defaults_to_none():
    config = _mock_load_config(mock_yaml_content)
    assert config.agencies[0].feeds[0].poll_interval_seconds is None


def test_poll_interval_set_correctly():
    config = _mock_load_config(mock_yaml_content_poll_interval_is_30)
    assert config.agencies[0].feeds[0].poll_interval_seconds == 30


def test_poll_interval_rejects_negative():
    with pytest.raises(ValidationError):
        _mock_load_config(mock_yaml_content_poll_interval_is_neg_1)


def test_poll_interval_rejects_zero():
    with pytest.raises(ValidationError):
        _mock_load_config(mock_yaml_content_poll_interval_is_zero)


mock_yaml_content_with_mdb_feed_id = """
writer:
  landing_dir: ./archive
  curated_dir: ./curated

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
    timezone: America/Los_Angeles
    base_url: https://api.bart.gov/gtfsrt
    auth:
      type: none
    mdb_feed_id: mdb-1234
    feeds:
      - name: bart-trips
        path: /tripupdate.aspx
"""


def test_mdb_feed_id_defaults_to_none():
    config = _mock_load_config(mock_yaml_content)
    assert config.agencies[0].mdb_feed_id is None


def test_mdb_feed_id_set_correctly():
    config = _mock_load_config(mock_yaml_content_with_mdb_feed_id)
    assert config.agencies[0].mdb_feed_id == "mdb-1234"
