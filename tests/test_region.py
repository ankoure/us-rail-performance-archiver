import zoneinfo

import pytest

from archiver.region import TIMEZONE_TO_CONTINENT, belongs_to_continent, continent_for


@pytest.mark.parametrize(
    "timezone, expected",
    [
        ("America/Chicago", "us"),
        ("Europe/Berlin", "eu"),
        ("Australia/Sydney", "au"),
    ],
)
def test_continent_for_known_zone(timezone, expected):
    assert continent_for(timezone) == expected


def test_continent_for_unmapped_zone_raises():
    with pytest.raises(ValueError):
        continent_for("Mars/Olympus_Mons")


@pytest.mark.parametrize(
    "timezone, continent, expected",
    [
        ("America/Chicago", "us", True),
        ("America/Chicago", "eu", False),
        ("Europe/Berlin", "eu", True),
        ("Europe/Berlin", "au", False),
    ],
)
def test_belongs_to_continent_agrees_with_continent_for(timezone, continent, expected):
    assert belongs_to_continent(timezone, continent) is expected


def test_belongs_to_continent_unmapped_zone_raises():
    with pytest.raises(ValueError):
        belongs_to_continent("Mars/Olympus_Mons", "us")


def test_timezone_to_continent_matches_zoneinfo():
    # Regression guard: catches TIMEZONE_TO_CONTINENT silently drifting out of
    # sync if the installed tzdata ever adds or removes a zone.
    assert set(TIMEZONE_TO_CONTINENT) == zoneinfo.available_timezones()


def test_timezone_to_continent_values_are_valid_boxes():
    assert set(TIMEZONE_TO_CONTINENT.values()) == {"us", "eu", "au"}
