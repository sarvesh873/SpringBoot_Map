"""
Graph builder — orchestrates all parsers and stitches results together.

Pipeline:
  1. Parse pom.xml / build.gradle  →  project metadata
  2. Parse application.yml         →  runtime config
  3. Parse all .java files         →  ClassNode objects (AST + regex fallback)
  4. Parse OpenAPI YAML files      →  virtual ClassNode objects
  5. Parse .proto files            →  gRPC service ClassNode objects
  6. Interface-driven discovery    →  propagate endpoints from interfaces to impls
  7. Constructor-injection detect  →  fill missing is_injected flags
  8. Dependency resolution         →  populate dependencies / dependents edges
  9. Save manifest.json            →  enable future incremental updates
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from springmap.graph.models import ClassNode, MethodInfo, NodeType, ProjectConfig, ProjectGraph
from springmap.parser.config_parser import parse_app_config
from springmap.parser.java_parser import find_java_files, parse_java_file
from springmap.parser.openapi_parser import parse_all_openapi
from springmap.parser.pom_parser import parse_pom
from springmap.parser.proto_parser import parse_all_proto

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Manifest helpers
# ─────────────────────────────────────────────

def _file_hash(path: str) -> str:
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            h.update(f.read())
    except OSError:
        pass
    return h.hexdigest()


def load_manifest(out_dir: Path) -> dict[str, str]:
    mf = out_dir / "manifest.json"
    if mf.exists():
        try:
            return json.loads(mf.read_text())
        except Exception:
            pass
    return {}


def save_manifest(out_dir: Path, manifest: dict[str, str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


# ─────────────────────────────────────────────
# Post-parse passes
# ─────────────────────────────────────────────

def _interface_endpoint_discovery(graph: ProjectGraph) -> None:
    """
    Interface-driven endpoint discovery.

    When a controller class implements an interface that carries @RequestMapping
    or @*Mapping annotations (common in generated OpenAPI stubs, Feign clients,
    or custom API contracts), copy those endpoint methods to the implementing
    class so they appear in the graph even when the Java source only delegates.

    Example:
        @RestController
        class UserController implements UserApi {   // UserApi has @GetMapping methods
            @Override public UserDTO getUser(...) { ... }
        }
    The interface UserApi's endpoints will be inherited by UserController.
    """
    # Step 1: collect interfaces that have endpoints
    iface_endpoints: dict[str, list[MethodInfo]] = {}
    iface_base_paths: dict[str, Optional[str]] = {}

    for cls in graph.classes.values():
        if cls.is_interface and cls.endpoints:
            iface_endpoints[cls.name] = cls.endpoints
            iface_base_paths[cls.name] = cls.base_path

    if not iface_endpoints:
        return

    # Step 2: propagate to implementing classes
    for cls in graph.classes.values():
        if cls.is_interface:
            continue
        for iface_ref in cls.implements:
            bare = iface_ref.split("<")[0].strip()
            if bare not in iface_endpoints:
                continue

            existing_names = {m.name for m in cls.methods}
            inherited_base = iface_base_paths.get(bare)
            # Prefer the class's own base_path; fall back to interface's
            effective_base = cls.base_path or inherited_base

            for ep in iface_endpoints[bare]:
                if ep.name in existing_names:
                    # Method exists — ensure endpoint metadata is set
                    for m in cls.methods:
                        if m.name == ep.name and m.http_method is None:
                            m.http_method = ep.http_method
                            m.http_path = (effective_base or "").rstrip("/") + (ep.http_path or "")
                            m.request_body_type = ep.request_body_type
                    continue

                # Method not in class — add a copy with corrected path
                from copy import deepcopy
                new_ep = deepcopy(ep)
                if effective_base and new_ep.http_path:
                    if not new_ep.http_path.startswith(effective_base):
                        new_ep.http_path = effective_base.rstrip("/") + new_ep.http_path
                cls.methods.append(new_ep)
                log.debug(
                    "Interface discovery: added %s.%s from interface %s",
                    cls.name, new_ep.name, bare,
                )


def _detect_constructor_injection(graph: ProjectGraph) -> None:
    """
    Spring Boot 2.x+ encourages constructor injection without @Autowired.
    If a class has no @Autowired fields but has fields whose types match
    known Spring beans in the graph, mark them as injected.
    """
    known = {node.name for node in graph.classes.values()}

    for node in graph.classes.values():
        already = any(f.is_injected for f in node.fields)
        if already:
            continue
        for field in node.fields:
            bare = field.type.split("<")[0].strip()
            if bare in known and bare != node.name:
                field.is_injected = True


def _resolve_dependencies(graph: ProjectGraph) -> None:
    """
    Build the dependency graph by resolving injected field types to ClassNodes.

    Populates for each ClassNode:
      .dependencies  — list of class names this node depends on (injects)
      .dependents    — list of class names that depend on (inject) this node
    """
    # Multi-key index: bare class name → ClassNode
    index: dict[str, ClassNode] = {}
    for node in graph.classes.values():
        index[node.name] = node
        # Also index by interface names this class implements
        for iface in node.implements:
            bare = iface.split("<")[0].strip()
            if bare not in index:
                index[bare] = node

    # Reset any stale data
    for node in graph.classes.values():
        node.dependencies = []
        node.dependents = []

    for node in graph.classes.values():
        deps: list[str] = []

        # Source 1: injected fields
        for field in node.fields:
            if not field.is_injected:
                continue
            bare = field.type.split("<")[0].strip()
            target = index.get(bare)
            if target and target.name != node.name and target.name not in deps:
                deps.append(target.name)

        # Source 2: method-level call targets already resolved by java_parser
        for method in node.methods:
            for call in method.calls:
                # "UserRepository.findById()" → "UserRepository"
                cls_name = call.split(".")[0]
                target = index.get(cls_name)
                if target and target.name != node.name and target.name not in deps:
                    deps.append(target.name)

        node.dependencies = deps

        # Reverse edge
        for dep_name in deps:
            dep_node = index.get(dep_name)
            if dep_node and node.name not in dep_node.dependents:
                dep_node.dependents.append(node.name)


# ─────────────────────────────────────────────
# Core build logic
# ─────────────────────────────────────────────

def _make_config(raw: dict) -> ProjectConfig:
    return ProjectConfig(
        server_port=raw["server_port"],
        context_path=raw["context_path"],
        active_profiles=raw["active_profiles"],
        datasource_url=raw["datasource_url"],
        datasource_driver=raw["datasource_driver"],
        jpa_ddl_auto=raw["jpa_ddl_auto"],
        jpa_show_sql=raw["jpa_show_sql"],
        custom_props=raw["custom_props"],
    )


def _parse_java_files(
    project_root: str,
    out_dir: Path,
    existing_manifest: Optional[dict[str, str]] = None,
) -> tuple[dict[str, ClassNode], dict[str, str]]:
    """
    Parse all .java files (or only changed ones if existing_manifest is given).
    Returns (class_map, new_manifest).
    """
    java_files = find_java_files(project_root)
    manifest: dict[str, str] = dict(existing_manifest or {})
    class_map: dict[str, ClassNode] = {}

    if existing_manifest is not None:
        to_parse = [f for f in java_files if _file_hash(f) != existing_manifest.get(f)]
        label = f"Updating {len(to_parse)} changed Java file(s)"
    else:
        to_parse = java_files
        label = f"Parsing {len(to_parse)} Java file(s)"

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task(label, total=len(to_parse))
        for jf in to_parse:
            node = parse_java_file(jf, project_root)
            if node:
                class_map[node.name] = node
            manifest[jf] = _file_hash(jf)
            progress.advance(task)

    return class_map, manifest


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def build_graph(project_root: str, out_dir: Path) -> ProjectGraph:
    """Full build — parse everything from scratch."""
    log.info("Building SpringMap graph for: %s", project_root)

    pom = parse_pom(project_root)
    raw_cfg = parse_app_config(project_root)

    graph = ProjectGraph(
        project_name=pom["project_name"],
        base_package=pom["group_id"],
        java_version=pom["java_version"],
        spring_boot_version=pom["spring_boot_version"],
        maven_dependencies=pom["dependencies"],
        config=_make_config(raw_cfg),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        source_root=project_root,
    )

    # Java source files
    class_map, manifest = _parse_java_files(project_root, out_dir)
    graph.classes.update(class_map)

    # OpenAPI specs
    for node in parse_all_openapi(project_root):
        graph.classes[node.name] = node

    # .proto files
    for node in parse_all_proto(project_root):
        if node.name not in graph.classes:  # don't override a Java class with the same name
            graph.classes[node.name] = node

    # Post-parse passes
    _interface_endpoint_discovery(graph)
    _detect_constructor_injection(graph)
    _resolve_dependencies(graph)

    save_manifest(out_dir, manifest)
    return graph


def update_graph(project_root: str, out_dir: Path, existing: ProjectGraph) -> tuple[ProjectGraph, int]:
    """
    Incremental update — re-parse only Java files that changed since last build.
    OpenAPI and proto files are always re-parsed (they're typically small).
    Returns (updated_graph, changed_count).
    """
    old_manifest = load_manifest(out_dir)
    java_files = find_java_files(project_root)

    changed = [f for f in java_files if _file_hash(f) != old_manifest.get(f)]
    deleted_rels = {
        v.file_path for v in existing.classes.values()
        if v.source == "java"
           and not Path(project_root, v.file_path).exists()
    }

    if not changed and not deleted_rels:
        # Still re-scan OpenAPI/proto in case they changed
        _refresh_non_java(project_root, existing)
        return existing, 0

    # Remove deleted classes
    if deleted_rels:
        existing.classes = {
            k: v for k, v in existing.classes.items()
            if v.file_path not in deleted_rels
        }

    # Re-parse changed Java files
    class_map, new_manifest = _parse_java_files(
        project_root, out_dir, existing_manifest=old_manifest
    )
    existing.classes.update(class_map)

    # Refresh OpenAPI / proto
    _refresh_non_java(project_root, existing)

    # Re-run all post-parse passes
    _interface_endpoint_discovery(existing)
    _detect_constructor_injection(existing)
    _resolve_dependencies(existing)

    existing.generated_at = datetime.now().isoformat(timespec="seconds")
    save_manifest(out_dir, new_manifest)
    return existing, len(changed) + len(deleted_rels)


def _refresh_non_java(project_root: str, graph: ProjectGraph) -> None:
    """Remove and re-add all OpenAPI / proto derived nodes."""
    graph.classes = {
        k: v for k, v in graph.classes.items()
        if v.source == "java"
    }
    for node in parse_all_openapi(project_root):
        graph.classes[node.name] = node
    for node in parse_all_proto(project_root):
        if node.name not in graph.classes:
            graph.classes[node.name] = node