"""
Markdown exporter — generates GRAPH.md.

This file must be structured so that Copilot can answer questions
WITHOUT reading any Java source files. The key insight is specificity:
  - Full method signatures with parameter types
  - Exact call chains (method X calls service.methodY())
  - DI map: who injects whom
  - Endpoint → handler → service → repo chain in one place

The opening block tells Copilot HOW to use this file.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from springmap.graph.models import ClassNode, MethodInfo, NodeType, ProjectGraph


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _md_safe(text: str) -> str:
    """Escape pipe characters in table cells."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _short(type_str: str) -> str:
    """Return the bare class name from a qualified type, preserve generics."""
    # com.example.dto.UserDTO → UserDTO, but keep List<UserDTO>
    return re.sub(r"\b[a-z][\w.]+\.([A-Z])", r"\1", type_str)


def _dep_chain(cls: ClassNode, graph: ProjectGraph, depth: int = 0, visited: Optional[set] = None) -> str:
    """Build a text dependency chain like: A → B → C"""
    if visited is None:
        visited = set()
    if cls.name in visited or depth > 4:
        return cls.name
    visited.add(cls.name)
    if not cls.dependencies:
        return cls.name
    sub_chains = []
    for dep_name in cls.dependencies[:3]:  # limit breadth
        dep_node = graph.classes.get(dep_name)
        if dep_node:
            sub_chains.append(_dep_chain(dep_node, graph, depth + 1, visited))
        else:
            sub_chains.append(dep_name)
    return cls.name + " → " + ", ".join(sub_chains)


# ─────────────────────────────────────────────
# Section builders
# ─────────────────────────────────────────────

def _section_header(graph: ProjectGraph) -> str:
    cfg = graph.config
    db_info = f"`{cfg.datasource_url}`" if cfg.datasource_url else "not configured"
    profiles = ", ".join(cfg.active_profiles) if cfg.active_profiles else "default"

    ctrl_count = len(graph.controllers)
    svc_count = len(graph.services)
    repo_count = len(graph.repositories)
    entity_count = len(graph.entities)
    total = len(graph.classes)

    rest_count = len(graph.all_endpoints)
    grpc_count = len(graph.grpc_services)
    listener_count = len(graph.all_listeners)

    extra_rows = ""
    if grpc_count:
        extra_rows += f"| gRPC Services | {grpc_count} |\n"
    if listener_count:
        extra_rows += f"| Event Listeners | {listener_count} |\n"

    return f"""# 🗺️ SpringMap — {graph.project_name} Knowledge Graph

> **Generated**: {graph.generated_at}  
> **Java**: {graph.java_version or "unknown"} · **Spring Boot**: {graph.spring_boot_version or "unknown"}  
> **Base Package**: `{graph.base_package}`

---

## 📌 HOW TO USE THIS FILE (read before anything else)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  COPILOT / AI ASSISTANT INSTRUCTIONS                                     │
│                                                                           │
│  This file is the complete knowledge graph of the codebase.              │
│  Before reading ANY .java file, search this document first.              │
│                                                                           │
│  • Need a REST endpoint?           → See SECTION 2: REST Endpoints       │
│  • Need a gRPC RPC?                → See SECTION 3: gRPC Services        │
│  • Need a Kafka/Rabbit consumer?   → See SECTION 4: Event Listeners      │
│  • Need to know what a class does? → Search class name below            │
│  • Need a call chain for a feature? → Methods include "Calls:" rows      │
│  • Need entity fields?             → See the Entities section            │
│  • Need config values?             → See the Configuration section       │
│                                                                           │
│  Only open a source file when you need the actual implementation body.   │
│  Class structure, signatures, and dependencies are ALL in this file.     │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 1 · Project Overview

| Attribute | Value |
|-----------|-------|
| Controllers | {ctrl_count} |
| Services | {svc_count} |
| Repositories | {repo_count} |
| Entities | {entity_count} |
| REST Endpoints | {rest_count} |
{extra_rows}| Total Classes | {total} |
| Server Port | `{cfg.server_port}` |
| Context Path | `{cfg.context_path or "/"}` |
| Active Profiles | `{profiles}` |
| Database | {db_info} |
| JPA DDL | `{cfg.jpa_ddl_auto or "none"}` |

"""


def _section_endpoints(graph: ProjectGraph) -> str:
    endpoints = graph.all_endpoints
    if not endpoints:
        return ""

    rows: list[str] = []
    for cls, method in sorted(endpoints, key=lambda t: (t[1].http_method or "", t[1].http_path or "")):
        params = ", ".join(_short(str(p)) for p in method.parameters)
        req_body = f" `{_short(method.request_body_type)}`" if method.request_body_type else ""
        ret = _short(method.return_type)
        path = method.http_path or "/"
        rows.append(
            f"| `{method.http_method}` | `{_md_safe(path)}` "
            f"| {cls.name} | `{method.name}({_md_safe(params)})` "
            f"|{req_body} | `{_md_safe(ret)}` |"
        )

    table = "\n".join(rows)
    return f"""---

## 2 · REST Endpoints

| Method | Path | Controller | Handler | Request Body | Response |
|--------|------|------------|---------|--------------|----------|
{table}

"""


def _section_grpc(graph: ProjectGraph) -> str:
    """gRPC service RPCs from .proto files — distinct from REST, never mixed in."""
    rpcs = graph.all_grpc_methods
    if not rpcs:
        return ""

    rows: list[str] = []
    for cls, method in sorted(rpcs, key=lambda t: (t[0].name, t[1].name)):
        params = ", ".join(_short(str(p)) for p in method.parameters)
        ret = _short(method.return_type)
        rows.append(
            f"| {cls.name} | `{method.name}({_md_safe(params)})` | `{_md_safe(ret)}` | `{_md_safe(cls.file_path)}` |"
        )

    table = "\n".join(rows)
    return f"""---

## 3 · gRPC Services

| Service | RPC Method | Returns | Source |
|---------|------------|---------|--------|
{table}

"""


def _section_listeners(graph: ProjectGraph) -> str:
    """
    Kafka / RabbitMQ / SQS / JMS / @EventListener / @Scheduled methods.

    These are intentionally kept SEPARATE from REST Endpoints — a Kafka
    consumer is not an HTTP route, and treating it as one (or omitting it
    entirely) was a prior bug. Listed here so Copilot can find message
    consumers without grepping every @Service class.
    """
    listeners = graph.all_listeners
    if not listeners:
        return ""

    rows: list[str] = []
    for cls, method in sorted(listeners, key=lambda t: (t[1].http_method or "", t[0].name)):
        topic = method.http_path or "—"
        rows.append(
            f"| `{method.http_method}` | {_md_safe(topic)} | {cls.name} | `{method.name}()` | `{_md_safe(cls.file_path)}` |"
        )

    table = "\n".join(rows)
    return f"""---

## 4 · Event Listeners & Scheduled Jobs

| Type | Topic / Queue / Cron | Class | Handler | File |
|------|----------------------|-------|---------|------|
{table}

"""


def _section_controllers(graph: ProjectGraph) -> str:
    if not graph.controllers:
        return ""

    parts = ["---\n\n## 5 · Controllers\n"]
    for cls in graph.controllers:
        parts.append(_class_block(cls, graph, show_endpoints=True))
    return "\n".join(parts)


def _section_services(graph: ProjectGraph) -> str:
    if not graph.services:
        return ""

    parts = ["---\n\n## 6 · Services\n"]
    for cls in graph.services:
        parts.append(_class_block(cls, graph, show_endpoints=False))
    return "\n".join(parts)


def _section_repositories(graph: ProjectGraph) -> str:
    repos = graph.repositories
    if not repos:
        return ""

    parts = ["---\n\n## 7 · Repositories\n"]
    for cls in repos:
        parts.append(_repository_block(cls, graph))
    return "\n".join(parts)


def _section_entities(graph: ProjectGraph) -> str:
    entities = graph.entities
    if not entities:
        return ""

    parts = ["---\n\n## 8 · Entities\n"]
    for cls in entities:
        parts.append(_entity_block(cls))
    return "\n".join(parts)


def _section_other(graph: ProjectGraph) -> str:
    skip = {NodeType.CONTROLLER, NodeType.SERVICE, NodeType.REPOSITORY, NodeType.ENTITY,
            NodeType.OPENAPI, NodeType.GRPC}
    others = [c for c in sorted(graph.classes.values(), key=lambda c: c.name)
              if c.node_type not in skip]
    if not others:
        return ""

    parts = ["---\n\n## 9 · Other Components\n"]

    # Group by type
    by_type: dict[str, list[ClassNode]] = {}
    for cls in others:
        label = cls.node_type.value.title()
        by_type.setdefault(label, []).append(cls)

    for label, nodes in sorted(by_type.items()):
        parts.append(f"### {label}s\n")
        for cls in nodes:
            # Compact view for non-core classes
            parts.append(_compact_class_block(cls))

    return "\n".join(parts)


def _section_config(graph: ProjectGraph) -> str:
    cfg = graph.config
    parts = ["---\n\n## 10 · Configuration\n\n"]

    parts.append("| Key | Value |\n|-----|-------|\n")
    parts.append(f"| `server.port` | `{cfg.server_port}` |\n")
    if cfg.context_path:
        parts.append(f"| `server.servlet.context-path` | `{cfg.context_path}` |\n")
    if cfg.datasource_url:
        parts.append(f"| `spring.datasource.url` | `{_md_safe(cfg.datasource_url)}` |\n")
    if cfg.datasource_driver:
        parts.append(f"| `spring.datasource.driver-class-name` | `{cfg.datasource_driver}` |\n")
    if cfg.jpa_ddl_auto:
        parts.append(f"| `spring.jpa.hibernate.ddl-auto` | `{cfg.jpa_ddl_auto}` |\n")
    parts.append(f"| `spring.jpa.show-sql` | `{str(cfg.jpa_show_sql).lower()}` |\n")

    if cfg.custom_props:
        parts.append("\n**Custom properties:**\n\n| Key | Value |\n|-----|-------|\n")
        for k, v in sorted(cfg.custom_props.items()):
            parts.append(f"| `{k}` | `{_md_safe(v)}` |\n")

    return "".join(parts) + "\n"


def _section_dependency_graph(graph: ProjectGraph) -> str:
    """Textual call chain for each controller."""
    chains: list[str] = []
    for ctrl in graph.controllers:
        chain = _dep_chain(ctrl, graph)
        if chain:
            chains.append(f"- {chain}")

    if not chains:
        return ""

    return "---\n\n## 11 · Dependency Map\n\n```\n" + "\n".join(chains) + "\n```\n\n"


def _section_maven(graph: ProjectGraph) -> str:
    if not graph.maven_dependencies:
        return ""
    # Only show Spring-related deps to keep noise low
    spring_deps = [d for d in graph.maven_dependencies if "spring" in d.lower() or "boot" in d.lower()]
    other_deps = [d for d in graph.maven_dependencies if d not in spring_deps]

    lines = ["---\n\n## 12 · Key Dependencies\n\n**Spring / Boot:**\n"]
    for d in sorted(spring_deps)[:20]:
        lines.append(f"- `{d}`")
    if other_deps:
        lines.append("\n**Other:**")
        for d in sorted(other_deps)[:15]:
            lines.append(f"- `{d}`")
    return "\n".join(lines) + "\n\n"


# ─────────────────────────────────────────────
# Per-class renderers
# ─────────────────────────────────────────────

def _class_block(cls: ClassNode, graph: ProjectGraph, show_endpoints: bool) -> str:
    lines: list[str] = []
    ann_str = " ".join(str(a) for a in cls.annotations
                       if a.name not in {"Override", "SuppressWarnings"})
    lines.append(f"### {cls.name}\n")
    lines.append(f"**File**: `{cls.file_path}`  ")
    if ann_str:
        lines.append(f"**Annotations**: `{ann_str}`  ")
    if cls.base_path:
        lines.append(f"**Base Path**: `{cls.base_path}`  ")
    if cls.extends:
        lines.append(f"**Extends**: `{cls.extends}`  ")
    if cls.implements:
        lines.append(f"**Implements**: `{', '.join(cls.implements)}`  ")
    if cls.dependencies:
        lines.append(f"**Injects**: {', '.join(f'`{d}`' for d in cls.dependencies)}  ")
    if cls.dependents:
        lines.append(f"**Used by**: {', '.join(f'`{d}`' for d in cls.dependents[:6])}  ")
    lines.append("")

    if show_endpoints and cls.endpoints:
        lines.append("**Endpoints:**\n")
        lines.append("| HTTP | Path | Method | Body | Returns |")
        lines.append("|------|------|--------|------|---------|")
        for m in cls.endpoints:
            params = _md_safe(", ".join(_short(str(p)) for p in m.parameters))
            ret = _md_safe(_short(m.return_type))
            body = _md_safe(_short(m.request_body_type)) if m.request_body_type else "-"
            path = _md_safe(m.http_path or "/")
            lines.append(f"| `{m.http_method}` | `{path}` | `{m.name}({params})` | `{body}` | `{ret}` |")
        lines.append("")

    non_endpoint_methods = [m for m in cls.methods if not m.is_endpoint]
    if non_endpoint_methods:
        lines.append("**Methods:**\n")
        lines.append("| Signature | Calls | Flags |")
        lines.append("|-----------|-------|-------|")
        for m in non_endpoint_methods:
            sig = _md_safe(m.signature)
            calls = _md_safe(", ".join(m.calls[:4]) or "—")
            flags: list[str] = []
            if m.is_transactional:
                flags.append("@Transactional")
            if m.is_async:
                flags.append("@Async")
            if m.is_scheduled:
                flags.append("@Scheduled")
            flag_str = " ".join(flags) or "—"
            lines.append(f"| `{sig}` | {calls} | {flag_str} |")
        lines.append("")

    return "\n".join(lines)


def _repository_block(cls: ClassNode, graph: ProjectGraph) -> str:
    lines: list[str] = []
    lines.append(f"### {cls.name}\n")
    lines.append(f"**File**: `{cls.file_path}`  ")
    if cls.extends:
        lines.append(f"**Extends**: `{cls.extends}`  ")
    if cls.implements:
        lines.append(f"**Implements**: `{', '.join(cls.implements)}`  ")
    if cls.dependents:
        lines.append(f"**Used by**: {', '.join(f'`{d}`' for d in cls.dependents[:6])}  ")
    lines.append("")

    custom = [m for m in cls.methods if m.name not in {"findAll", "findById", "save", "delete"}]
    if custom:
        lines.append("**Custom Query Methods:**\n")
        for m in custom:
            params = ", ".join(_short(str(p)) for p in m.parameters)
            lines.append(f"- `{m.return_type} {m.name}({params})`")
        lines.append("")

    return "\n".join(lines)


def _entity_block(cls: ClassNode) -> str:
    lines: list[str] = []
    lines.append(f"### {cls.name}\n")
    lines.append(f"**File**: `{cls.file_path}`  ")
    if cls.table_name:
        lines.append(f"**Table**: `{cls.table_name}`  ")
    if cls.extends:
        lines.append(f"**Extends**: `{cls.extends}`  ")
    lines.append("")

    if cls.fields:
        lines.append("| Field | Type | Column | Notes |")
        lines.append("|-------|------|--------|-------|")
        for f in cls.fields:
            col = f.column_name or f.name
            notes: list[str] = []
            if f.is_id:
                notes.append("PK")
            if f.relationship:
                notes.append(f"@{f.relationship}")
            note_str = ", ".join(notes) if notes else "—"
            lines.append(f"| `{f.name}` | `{_short(f.type)}` | `{col}` | {note_str} |")
        lines.append("")

    return "\n".join(lines)


def _compact_class_block(cls: ClassNode) -> str:
    ann_str = " ".join(str(a) for a in cls.annotations
                       if a.name not in {"Override", "SuppressWarnings"})
    methods_str = ", ".join(
        f"`{m.name}()`" for m in cls.methods[:6]
    )
    lines = [f"**{cls.name}** — `{cls.file_path}`"]
    if ann_str:
        lines.append(f"  Annotations: `{ann_str}`")
    if cls.dependencies:
        lines.append(f"  Injects: {', '.join(f'`{d}`' for d in cls.dependencies)}")
    if methods_str:
        lines.append(f"  Methods: {methods_str}")
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def export_markdown(graph: ProjectGraph, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "GRAPH.md"

    sections = [
        _section_header(graph),
        _section_endpoints(graph),
        _section_grpc(graph),
        _section_listeners(graph),
        _section_controllers(graph),
        _section_services(graph),
        _section_repositories(graph),
        _section_entities(graph),
        _section_other(graph),
        _section_config(graph),
        _section_dependency_graph(graph),
        _section_maven(graph),
    ]

    content = "\n".join(s for s in sections if s)
    out_path.write_text(content, encoding="utf-8")
    return out_path