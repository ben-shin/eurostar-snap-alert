from pathlib import Path

import pytest

from eurostar_alerts.config import load_config


BASE_CONFIG = """
settings:
  timezone: Europe/London
checks:
  snap: true
  normal_eurostar: false
routes:
  - name: Brussels to London
    origin: Brussels Midi
    destination: London St Pancras
    start_date: 2026-08-17
    end_date: 2026-08-19
    passengers: 1
"""


def write_config(tmp_path: Path, contents: str = BASE_CONFIG) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(contents, encoding="utf-8")
    return path


def test_load_config_parses_route_and_defaults(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))

    assert config.settings.user_agent == ""
    assert config.routes[0].name == "Brussels to London"
    assert config.routes[0].start_date.isoformat() == "2026-08-17"


def test_load_config_rejects_unknown_timezone(tmp_path: Path) -> None:
    contents = BASE_CONFIG.replace("Europe/London", "Mars/Olympus")

    with pytest.raises(ValueError, match="Unknown timezone"):
        load_config(write_config(tmp_path, contents))


def test_load_config_requires_an_enabled_check(tmp_path: Path) -> None:
    contents = BASE_CONFIG.replace("snap: true", "snap: false")

    with pytest.raises(ValueError, match="Enable at least one check"):
        load_config(write_config(tmp_path, contents))
