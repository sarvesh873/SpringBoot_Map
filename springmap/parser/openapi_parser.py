"""
OpenAPI / Swagger YAML parser.

Finds all OpenAPI spec files in the project, parses them, and produces
virtual ClassNode objects (one per API tag) so the graph includes REST
contracts that may not have explicit @RequestMapping annotations in Java
source (e.g. code-generated controllers, Feign client specs, gateway routes).
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

from springmap.graph.models import (
    AnnotationInfo,
    ClassNode,
    MethodInfo,
    NodeType,
    ParamInfo,
)

# HTTP methods in OpenAPI spec
_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}


# ─────────────────────────────────────────────
# File discovery
#
# BUG FIX: the old version only checked a fixed list of top-level dirs with
# non-recursive .glob(), so files nested deeper than one level — e.g.
# src/main/resources/api/openapi.yaml, src/main/resources/contracts/v2/spec.yaml —
# were silently skipped. It also used a plain substring check for "openapi:"
# which fails on JSON specs where the key is quoted: {"openapi": "3.0.0"}
# contains openapi": not openapi: (extra quote breaks the match).
# ─────────────────────────────────────────────

# Directories we never want to walk into — build artifacts, deps, VCS internals
_EXCLUDED_DIR_NAMES = frozenset({
    "target", "build", "node_modules", ".git", ".idea", ".vscode",
    "out", "dist", ".gradle", ".mvn",
})

# Skip absurdly large files — a real OpenAPI spec is never this big
_MAX_SPEC_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def _is_excluded(path: Path) -> bool:
    return any(part in _EXCLUDED_DIR_NAMES or part.startswith(".") for part in path.parts)


def _is_openapi_file(path: Path) -> bool:
    """
    Reliable detection that works for both YAML and JSON specs.

    Strategy:
      1. Cheap pre-filter: does "openapi" or "swagger" appear anywhere in the
         first 4 KB (handles huge info/description blocks before the key)?
      2. Confirmation: parse the file with yaml.safe_load (a superset of JSON,
         so this works for .json specs too) and check for a top-level
         'openapi' or 'swagger' key — avoids both false positives (a comment
         that happens to mention "openapi") and false negatives (quoting
         differences between YAML and JSON).
    """
    try:
        if path.stat().st_size > _MAX_SPEC_SIZE_BYTES:
            return False
        snippet = path.read_bytes()[:4096].decode("utf-8", errors="replace").lower()
    except OSError:
        return False

    if "openapi" not in snippet and "swagger" not in snippet:
        return False

    if not HAS_YAML:
        # No yaml lib to confirm — trust the substring pre-filter
        return True

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = yaml.safe_load(f)
    except Exception:
        return False

    return isinstance(data, dict) and ("openapi" in data or "swagger" in data)


def find_openapi_files(project_root: str) -> list[Path]:
    """
    Recursively search the ENTIRE project (minus build/dependency dirs) for
    OpenAPI/Swagger spec files in .yaml, .yml, or .json format — regardless
    of how deeply nested they are or what they're named.
    """
    root = Path(project_root)
    found: list[Path] = []

    for pattern in ("*.yaml", "*.yml", "*.json"):
        for f in root.rglob(pattern):
            if _is_excluded(f):
                continue
            if _is_openapi_file(f):
                found.append(f)

    return found


# ─────────────────────────────────────────────
# Schema → type string
# ─────────────────────────────────────────────

def _schema_to_type(schema: dict) -> str:
    if not schema:
        return "Object"

    ref = schema.get("$ref", "")
    if ref:
        return ref.split("/")[-1]   # #/components/schemas/UserDTO → UserDTO

    fmt = schema.get("format", "")
    oa_type = schema.get("type", "")

    mapping = {
        ("integer", "int64"): "Long",
        ("integer", ""): "Integer",
        ("number", "float"): "Float",
        ("number", "double"): "Double",
        ("number", ""): "Double",
        ("string", "date"): "LocalDate",
        ("string", "date-time"): "LocalDateTime",
        ("string", ""): "String",
        ("boolean", ""): "Boolean",
        ("array", ""): "List",
    }
    java_type = mapping.get((oa_type, fmt)) or mapping.get((oa_type, "")) or "Object"

    if oa_type == "array":
        items = schema.get("items", {})
        inner = _schema_to_type(items)
        return f"List<{inner}>"

    return java_type


def _response_type(operation: dict) -> str:
    """Try to extract the primary success response type."""
    responses = operation.get("responses", {})
    for code in ("200", "201", "202", "default"):
        resp = responses.get(code, {})
        content = resp.get("content", {})
        for media_type in ("application/json", "*/*"):
            schema = content.get(media_type, {}).get("schema", {})
            if schema:
                return _schema_to_type(schema)
    return "void"


def _request_body_type(operation: dict) -> Optional[str]:
    rb = operation.get("requestBody", {})
    content = rb.get("content", {})
    for media_type in ("application/json", "*/*"):
        schema = content.get(media_type, {}).get("schema", {})
        if schema:
            return _schema_to_type(schema)
    return None


# ─────────────────────────────────────────────
# Parameter extraction
# ─────────────────────────────────────────────

def _extract_params(operation: dict) -> list[ParamInfo]:
    params: list[ParamInfo] = []
    for p in operation.get("parameters", []):
        pname = p.get("name", "param")
        pschema = p.get("schema", {})
        ptype = _schema_to_type(pschema) if pschema else "String"
        pin = p.get("in", "")
        ann = {"path": "PathVariable", "query": "RequestParam", "header": "RequestHeader"}.get(pin, "")
        params.append(ParamInfo(type=ptype, name=pname, annotations=[ann] if ann else []))

    rb_type = _request_body_type(operation)
    if rb_type:
        params.append(ParamInfo(type=rb_type, name="body", annotations=["RequestBody"]))

    return params


# ─────────────────────────────────────────────
# Main parsing
# ─────────────────────────────────────────────

def parse_openapi_file(path: Path, project_root: str) -> list[ClassNode]:
    """
    Parse one OpenAPI file.  Returns one ClassNode per tag (controller group).
    If a path has no tags, all its operations land in a catch-all "ApiSpec" node.
    """
    if not HAS_YAML:
        log.warning("PyYAML not installed — cannot parse OpenAPI file %s", path)
        return []

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            spec = yaml.safe_load(f) or {}
    except Exception as exc:
        log.warning("Cannot parse OpenAPI file %s: %s", path, exc)
        return []

    rel_path = str(path.relative_to(Path(project_root)))
    base_path: str = spec.get("servers", [{}])[0].get("url", "") if spec.get("servers") else ""

    # Remove protocol / host from base_path (keep only path component)
    base_path = re.sub(r"^https?://[^/]+", "", base_path)

    # Group operations by tag → one ClassNode per tag
    tag_methods: dict[str, list[MethodInfo]] = {}
    tag_base_paths: dict[str, str] = {}

    paths_section = spec.get("paths", {}) or {}
    for api_path, path_item in paths_section.items():
        if not isinstance(path_item, dict):
            continue
        for verb, operation in path_item.items():
            if verb not in _HTTP_VERBS:
                continue
            if not isinstance(operation, dict):
                continue

            tags = operation.get("tags", ["ApiSpec"])
            tag = tags[0] if tags else "ApiSpec"

            op_id = operation.get("operationId", "")
            # Derive method name from operationId or path
            if op_id:
                method_name = op_id[0].lower() + op_id[1:].replace("-", "_")
            else:
                slug = api_path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
                method_name = f"{verb}_{slug}" if slug else verb

            params = _extract_params(operation)
            ret_type = _response_type(operation)
            rb_type = _request_body_type(operation)
            full_path = base_path.rstrip("/") + api_path

            mi = MethodInfo(
                name=method_name,
                return_type=ret_type,
                parameters=params,
                http_method=verb.upper(),
                http_path=full_path,
                request_body_type=rb_type,
                annotations=[AnnotationInfo(
                    name=f"{verb.capitalize()}Mapping",
                    attributes={"value": full_path},
                )],
            )

            tag_methods.setdefault(tag, []).append(mi)

    if not tag_methods:
        return []

    nodes: list[ClassNode] = []
    for tag, methods in tag_methods.items():
        # Class name: UserController, OrderApiSpec, etc.
        class_name = (
            tag if tag.endswith(("Controller", "Api", "ApiSpec"))
            else f"{tag}ApiSpec"
        )
        node = ClassNode(
            name=class_name,
            package="openapi.generated",
            file_path=rel_path,
            node_type=NodeType.OPENAPI,
            methods=methods,
            source="openapi",
            annotations=[AnnotationInfo(name="OpenApiSpec", attributes={"file": rel_path})],
        )
        nodes.append(node)
        log.debug("OpenAPI: created virtual node %s with %d endpoints", class_name, len(methods))

    return nodes


def parse_all_openapi(project_root: str) -> list[ClassNode]:
    """Discover and parse all OpenAPI/Swagger files in the project."""
    files = find_openapi_files(project_root)
    if files:
        log.info("Found %d OpenAPI file(s): %s", len(files), [f.name for f in files])

    nodes: list[ClassNode] = []
    for f in files:
        nodes.extend(parse_openapi_file(f, project_root))
    return nodes