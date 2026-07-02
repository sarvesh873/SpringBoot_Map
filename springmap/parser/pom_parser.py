"""
Parse pom.xml to extract project metadata and dependencies.
Uses stdlib xml.etree — no extra deps.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Maven XML namespace
_NS = {"m": "http://maven.apache.org/POM/4.0.0"}

# Artifact IDs that indicate "this dependency IS the Spring Boot version anchor"
_SPRING_BOOT_BOM_ARTIFACTS = frozenset({
    "spring-boot-starter-parent",
    "spring-boot-dependencies",
})


def _find(root: ET.Element, *tags: str) -> Optional[str]:
    """Find the text of a nested element, trying both namespaced and bare."""
    for tag in tags:
        el = root.find("/".join(f"m:{t}" for t in tag.split("/")), _NS)
        if el is not None and el.text:
            return el.text.strip()
        el = root.find(tag)
        if el is not None and el.text:
            return el.text.strip()
    return None


def _resolve_property(value: Optional[str], properties: dict[str, str]) -> Optional[str]:
    """Resolve a Maven ${property.name} placeholder against the <properties> block."""
    if not value:
        return value
    m = re.fullmatch(r"\$\{([\w.\-]+)\}", value.strip())
    if m:
        return properties.get(m.group(1), value)
    return value


def _collect_properties(root: ET.Element) -> dict[str, str]:
    props_el = root.find("m:properties", _NS) or root.find("properties")
    result: dict[str, str] = {}
    if props_el is not None:
        for child in props_el:
            tag = child.tag.split("}")[-1]  # strip namespace if present
            if child.text:
                result[tag] = child.text.strip()
    return result


def _find_spring_boot_version(root: ET.Element, properties: dict[str, str]) -> str:
    """
    Spring Boot version fallback chain — handles both the common case
    (extends spring-boot-starter-parent directly) AND enterprise setups
    where an internal/corporate parent POM is used instead and Spring Boot
    is pulled in via a <dependencyManagement> BOM import or a direct
    dependency on spring-boot-starter-* artifacts.

    BUG FIX: the previous version only checked parent.artifactId, so any
    project using a company-internal parent POM (extremely common in
    enterprise codebases) silently reported an empty Spring Boot version
    even though the project clearly used Spring Boot.
    """
    # 1. Direct parent — the common open-source case
    parent = root.find("m:parent", _NS) or root.find("parent")
    if parent is not None:
        parent_artifact = (_find(parent, "artifactId") or "").lower()
        if "spring-boot" in parent_artifact:
            v = _find(parent, "version")
            resolved = _resolve_property(v, properties)
            if resolved:
                return resolved

    # 2. Common explicit property names
    for prop_name in ("spring-boot.version", "spring.boot.version", "springboot.version"):
        if prop_name in properties:
            return properties[prop_name]

    # 3. BOM import inside <dependencyManagement><dependencies>
    dm = root.find("m:dependencyManagement/m:dependencies", _NS) \
         or root.find("dependencyManagement/dependencies")
    if dm is not None:
        for dep in dm:
            group = _find(dep, "groupId") or ""
            artifact = _find(dep, "artifactId") or ""
            if group == "org.springframework.boot" and artifact in _SPRING_BOOT_BOM_ARTIFACTS:
                v = _resolve_property(_find(dep, "version"), properties)
                if v:
                    return v

    # 4. Any direct dependency on org.springframework.boot with an explicit version
    #    (covers projects that depend on spring-boot-starter-web etc. directly,
    #    without a BOM, and pin the version per-dependency)
    deps_root = root.find("m:dependencies", _NS) or root.find("dependencies")
    if deps_root is not None:
        for dep in deps_root:
            group = _find(dep, "groupId") or ""
            if group == "org.springframework.boot":
                v = _resolve_property(_find(dep, "version"), properties)
                if v:
                    return v

    return ""


def parse_pom(project_root: str) -> dict:
    """
    Returns a dict with keys:
      project_name, group_id, artifact_id, version,
      java_version, spring_boot_version, dependencies
    """
    result = {
        "project_name": "",
        "group_id": "",
        "artifact_id": "",
        "version": "",
        "java_version": "",
        "spring_boot_version": "",
        "dependencies": [],
    }

    pom = Path(project_root) / "pom.xml"
    if not pom.exists():
        _try_gradle(project_root, result)
        return result

    try:
        tree = ET.parse(str(pom))
        root = tree.getroot()
    except ET.ParseError as exc:
        log.warning("Could not parse pom.xml: %s", exc)
        return result

    properties = _collect_properties(root)

    result["group_id"] = _find(root, "groupId") or ""
    result["artifact_id"] = _find(root, "artifactId") or ""
    result["version"] = _resolve_property(_find(root, "version"), properties) or ""
    result["project_name"] = result["artifact_id"] or result["group_id"] or "Unknown"

    # Java version from properties (also resolve placeholders, in case one
    # property points at another)
    for tag in ("java.version", "maven.compiler.source", "maven.compiler.release"):
        if tag in properties:
            result["java_version"] = _resolve_property(properties[tag], properties) or properties[tag]
            break

    result["spring_boot_version"] = _find_spring_boot_version(root, properties)

    # Dependencies
    deps: list[str] = []
    deps_root = root.find("m:dependencies", _NS) or root.find("dependencies")
    if deps_root is not None:
        for dep in deps_root:
            group = _find(dep, "groupId") or ""
            artifact = _find(dep, "artifactId") or ""
            if group and artifact:
                deps.append(f"{group}:{artifact}")

    dm = root.find("m:dependencyManagement/m:dependencies", _NS) \
         or root.find("dependencyManagement/dependencies")
    if dm is not None:
        for dep in dm:
            group = _find(dep, "groupId") or ""
            artifact = _find(dep, "artifactId") or ""
            if group and artifact:
                label = f"{group}:{artifact}"
                if label not in deps:
                    deps.append(label)

    result["dependencies"] = sorted(set(deps))
    return result


def _try_gradle(project_root: str, result: dict) -> None:
    """Best-effort Gradle project name / version extraction."""
    settings = Path(project_root) / "settings.gradle"
    settings_kts = Path(project_root) / "settings.gradle.kts"
    for settings_file in (settings, settings_kts):
        if settings_file.exists():
            text = settings_file.read_text(errors="replace")
            m = re.search(r"rootProject\.name\s*=\s*['\"]([^'\"]+)['\"]", text)
            if m:
                result["project_name"] = m.group(1)
            break

    for build_name in ("build.gradle", "build.gradle.kts"):
        build = Path(project_root) / build_name
        if not build.exists():
            continue
        text = build.read_text(errors="replace")

        m = re.search(
            r"id\s*\(?['\"]org\.springframework\.boot['\"]\)?\s+version\s*\(?['\"]([^'\"]+)['\"]",
            text,
        )
        if m:
            result["spring_boot_version"] = m.group(1)

        m = re.search(r"sourceCompatibility\s*=\s*['\"]?(\d[\d.]+)['\"]?", text)
        if m:
            result["java_version"] = m.group(1)

        # Fallback: direct dependency declaration with explicit version
        if not result["spring_boot_version"]:
            m = re.search(
                r"org\.springframework\.boot:spring-boot[\w-]*:(\d[\w.\-]*)",
                text,
            )
            if m:
                result["spring_boot_version"] = m.group(1)
        if result["spring_boot_version"]:
            break