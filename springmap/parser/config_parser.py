"""
Parse application.yml and application.properties to extract
server config, datasource settings, and key custom properties.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def parse_app_config(project_root: str) -> dict:
    """
    Returns a dict with keys:
      server_port, context_path, datasource_url, datasource_driver,
      jpa_ddl_auto, jpa_show_sql, active_profiles, custom_props
    """
    result = {
        "server_port": "8080",
        "context_path": "",
        "datasource_url": None,
        "datasource_driver": None,
        "jpa_ddl_auto": None,
        "jpa_show_sql": False,
        "active_profiles": [],
        "custom_props": {},
    }

    resources = Path(project_root) / "src" / "main" / "resources"
    if not resources.exists():
        resources = Path(project_root)

    # Try YAML first, then properties
    for filename in ("application.yml", "application.yaml"):
        path = resources / filename
        if path.exists():
            _parse_yaml(path, result)
            return result

    props_path = resources / "application.properties"
    if props_path.exists():
        _parse_properties(props_path, result)

    return result


def _parse_yaml(path: Path, result: dict) -> None:
    if not HAS_YAML:
        log.warning("PyYAML not installed — skipping %s", path)
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        log.warning("Cannot parse %s: %s", path, exc)
        return

    server = data.get("server", {}) or {}
    result["server_port"] = str(server.get("port", "8080"))
    result["context_path"] = str(server.get("servlet", {}).get("context-path", "") or "")

    spring = data.get("spring", {}) or {}
    ds = spring.get("datasource", {}) or {}
    result["datasource_url"] = ds.get("url")
    result["datasource_driver"] = ds.get("driver-class-name")

    jpa = spring.get("jpa", {}) or {}
    hibernate = jpa.get("hibernate", {}) or {}
    result["jpa_ddl_auto"] = hibernate.get("ddl-auto")
    result["jpa_show_sql"] = bool(jpa.get("show-sql", False))

    profiles = spring.get("profiles", {}) or {}
    active = profiles.get("active", "")
    if isinstance(active, list):
        result["active_profiles"] = active
    elif active:
        result["active_profiles"] = [p.strip() for p in str(active).split(",")]

    # Collect custom top-level keys (anything not spring/server/logging/management)
    skip_keys = {"spring", "server", "logging", "management", "info"}
    for key, val in data.items():
        if key not in skip_keys and isinstance(val, dict):
            for subkey, subval in val.items():
                if isinstance(subval, (str, int, bool)):
                    result["custom_props"][f"{key}.{subkey}"] = str(subval)


def _parse_properties(path: Path, result: dict) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Cannot read %s: %s", path, exc)
        return

    props: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            props[k.strip()] = v.strip()

    result["server_port"] = props.get("server.port", "8080")
    result["context_path"] = props.get("server.servlet.context-path", "")
    result["datasource_url"] = props.get("spring.datasource.url")
    result["datasource_driver"] = props.get("spring.datasource.driver-class-name")
    result["jpa_ddl_auto"] = props.get("spring.jpa.hibernate.ddl-auto")
    result["jpa_show_sql"] = props.get("spring.jpa.show-sql", "false").lower() == "true"

    active = props.get("spring.profiles.active", "")
    result["active_profiles"] = [p.strip() for p in active.split(",") if p.strip()]

    skip_prefixes = {"server.", "spring.", "logging.", "management.", "info."}
    for k, v in props.items():
        if not any(k.startswith(p) for p in skip_prefixes):
            result["custom_props"][k] = v