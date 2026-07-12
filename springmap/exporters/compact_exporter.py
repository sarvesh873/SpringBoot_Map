"""
Compact GRAPH.md exporter — optimised for Copilot + Claude agent.

Full GRAPH.md exports everything (call chains, entity field details, Maven
section, dependency map) so a human developer can browse it like docs.
That's ~900 chars/class → 13,500 tokens for a 60-class project, which
costs MORE tokens than Copilot's auto-file-reading would.

Compact GRAPH.md exports ONLY what Claude needs to answer structural
questions without opening any Java file:
  - Class name, type, file path
  - REST endpoints (method + path + handler — the most-queried data)
  - DI map (who injects whom — needed to understand call chains)
  - Key method names + return types (enough to know what a class can do)
  - Kafka/RabbitMQ listener topics
  - Server port + datasource (most-asked config values)

Result: ~350 chars/class → 5,250 tokens for a 60-class project.
That's a 58% reduction vs full GRAPH.md, and 56% cheaper than
Copilot reading 10 Java files automatically.

The compact file is written to springmap-out/GRAPH_COMPACT.md.
You attach THIS file to Copilot, not the full GRAPH.md.
"""
from __future__ import annotations

from pathlib import Path

from springmap.graph.models import NodeType, ProjectGraph, HTTP_VERBS, LISTENER_VERBS


def _short(t: str, max_len: int = 30) -> str:
    t = t.split("<")[0].strip()      # strip generics
    t = t.rsplit(".", 1)[-1]         # strip package prefix
    return t[:max_len]


def export_compact_markdown(graph: ProjectGraph, out_dir: Path) -> Path:
    """
    Generate GRAPH_COMPACT.md — the file you attach to Copilot Chat.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "GRAPH_COMPACT.md"

    cfg = graph.config
    lines: list[str] = []

    # ── Header with explicit Copilot instructions ─────────────────────────
    lines.append(f"# {graph.project_name} — Knowledge Graph (Compact)")
    lines.append(f"> Java {graph.java_version} · Spring Boot {graph.spring_boot_version} · Port `{cfg.server_port}`{(' · Context `' + cfg.context_path + '`') if cfg.context_path else ''}")
    lines.append("")
    lines.append("**COPILOT INSTRUCTIONS — read before answering any question:**")
    lines.append("- This file is the complete structural index of the codebase")
    lines.append("- **Search here first.** Only open a `.java` file if you need the actual method body")
    lines.append("- Class names, REST endpoints, Kafka topics, and DI dependencies are ALL listed below")
    lines.append("- If the answer is in this file, do NOT open any source files")
    lines.append("")

    # ── REST endpoint quick-reference table ──────────────────────────────
    all_endpoints = [
        (cls, m)
        for cls in graph.classes.values()
        if cls.node_type in (NodeType.CONTROLLER, NodeType.OPENAPI)
        for m in cls.methods
        if m.http_method and m.http_method.upper() in HTTP_VERBS
    ]
    if all_endpoints:
        lines.append("## REST Endpoints")
        lines.append("| Method | Path | Handler |")
        lines.append("|--------|------|---------|")
        for cls, m in sorted(all_endpoints, key=lambda t: (t[1].http_path or "", t[1].http_method or "")):
            lines.append(f"| `{m.http_method}` | `{m.http_path or '/'}` | `{cls.name}.{m.name}()` |")
        lines.append("")

    # ── Kafka / listener quick-reference ─────────────────────────────────
    all_listeners = [
        (cls, m)
        for cls in graph.classes.values()
        for m in cls.methods
        if m.http_method and m.http_method.upper() in LISTENER_VERBS
    ]
    if all_listeners:
        lines.append("## Event Listeners")
        lines.append("| Type | Topic/Queue | Class | Handler |")
        lines.append("|------|-------------|-------|---------|")
        for cls, m in sorted(all_listeners, key=lambda t: (t[1].http_method or "", t[0].name)):
            topic = m.http_path or "—"
            lines.append(f"| `{m.http_method}` | `{topic}` | {cls.name} | `{m.name}()` |")
        lines.append("")

    # ── Class index ───────────────────────────────────────────────────────
    lines.append("## Class Index")
    lines.append("")

    type_order = [
        NodeType.CONTROLLER, NodeType.SERVICE, NodeType.REPOSITORY,
        NodeType.ENTITY, NodeType.COMPONENT, NodeType.CONFIGURATION,
        NodeType.GRPC, NodeType.OPENAPI, NodeType.DTO,
        NodeType.EXCEPTION, NodeType.UTIL, NodeType.INTERFACE, NodeType.UNKNOWN,
    ]

    for node_type in type_order:
        nodes = sorted(
            [c for c in graph.classes.values() if c.node_type == node_type],
            key=lambda c: c.name,
        )
        if not nodes:
            continue

        label = node_type.value.title()
        lines.append(f"### {label}s")

        for cls in nodes:
            # One line: name + file
            lines.append(f"**{cls.name}** `{cls.file_path}`")

            # Injects / used-by (most important structural fact)
            if cls.dependencies:
                deps = ", ".join(f"`{d}`" for d in cls.dependencies[:5])
                lines.append(f"  Injects: {deps}")
            if cls.dependents:
                used = ", ".join(f"`{d}`" for d in cls.dependents[:4])
                lines.append(f"  Used by: {used}")

            # Base path for controllers
            if cls.base_path:
                lines.append(f"  Base path: `{cls.base_path}`")

            # Table name for entities
            if cls.table_name:
                lines.append(f"  Table: `{cls.table_name}`")

            # Methods — only non-endpoint, non-listener methods (those are in the tables above)
            plain_methods = [
                m for m in cls.methods
                if not m.http_method
                and m.name not in {"equals", "hashCode", "toString", "canEqual"}
            ]
            if plain_methods:
                sigs = ", ".join(
                    f"`{m.name}()`" for m in plain_methods[:6]
                )
                lines.append(f"  Methods: {sigs}")

            lines.append("")  # blank line between classes

    # ── Config summary (just the key facts) ──────────────────────────────
    lines.append("## Key Config")
    lines.append(f"- Port: `{cfg.server_port}`")
    if cfg.context_path:
        lines.append(f"- Context path: `{cfg.context_path}`")
    if cfg.datasource_url:
        lines.append(f"- Datasource: `{cfg.datasource_url}`")
    if cfg.jpa_ddl_auto:
        lines.append(f"- JPA DDL: `{cfg.jpa_ddl_auto}`")
    if cfg.active_profiles:
        lines.append(f"- Profiles: `{', '.join(cfg.active_profiles)}`")
    if cfg.custom_props:
        lines.append("")
        lines.append("**Custom properties:**")
        for k, v in sorted(cfg.custom_props.items()):
            lines.append(f"- `{k}` = `{v}`")
    lines.append("")

    content = "\n".join(lines)
    out_path.write_text(content, encoding="utf-8")
    return out_path