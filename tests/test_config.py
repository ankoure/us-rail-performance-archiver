from pydantic import ValidationError
import pytest

from unittest.mock import patch, mock_open

from archiver.loader import load_config

mock_yaml_content = """
writer:
  base_dir: ./archive

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
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
  base_dir: ./archive

telemetry:
  enbaled: true   # the typo we're catching

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
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
  base_dir: ./archive

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
