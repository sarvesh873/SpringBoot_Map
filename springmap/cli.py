"""
SpringMap CLI — entry point for all commands.

All commands share a --out option that controls the output directory
(default: ./springmap-out relative to CWD).

Build / Update write to that directory.
Query / Show / Path / Endpoints / Stats read from it.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import click
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

console = Console()
err_console = Console(stderr=True)

# HTTP method colors
METHOD_STYLE: dict[str, str] = {
    "GET": "bright_green",
    "POST": "bright_blue",
    "PUT": "bright_yellow",
    "DELETE": "bright_red",
    "PATCH": "cyan",
    "REQUEST": "magenta",
    "RPC": "bright_magenta",
    "KAFKA": "orange3",
    "RABBIT": "orange3",
    "SQS": "orange3",
    "JMS": "orange3",
    "EVENT": "yellow",
    "SCHEDULED": "bright_black",
    "STREAM": "orange3",
}


def _short_type(type_str: str) -> str:
    """Shorten a fully-qualified Java type for display.

    com.example.dto.UserDTO        → UserDTO
    java.util.List<UserDTO>        → List<UserDTO>
    Page<com.example.dto.UserDTO>  → Page<UserDTO>
    """
    import re
    shortened = re.sub(r"\b(?:[a-z][\w]*\.)+([A-Z]\w*)", r"\1", type_str or "")
    return shortened or (type_str or "void")

# NodeType colors / labels
TYPE_STYLE: dict[str, tuple[str, str]] = {
    "controller": ("bright_blue", "Controller"),
    "service": ("bright_green", "Service"),
    "repository": ("bright_yellow", "Repository"),
    "entity": ("bright_magenta", "Entity"),
    "component": ("cyan", "Component"),
    "configuration": ("bright_cyan", "Config"),
    "dto": ("white", "DTO"),
    "exception": ("bright_red", "Exception"),
    "util": ("dim white", "Util"),
    "main": ("bright_white", "Main"),
    "interface": ("dim cyan", "Interface"),
    "openapi": ("bright_blue", "OpenAPI"),
    "grpc": ("magenta", "gRPC"),
    "unknown": ("dim", "Unknown"),
}


def _type_badge(node_type: str) -> Text:
    style, label = TYPE_STYLE.get(node_type, ("dim", node_type.title()))
    return Text(f" {label} ", style=f"bold {style} on default")


def _method_badge(method: str) -> Text:
    style = METHOD_STYLE.get(method.upper(), "white")
    return Text(f" {method:<6} ", style=f"bold {style}")


def _out_path(ctx: click.Context) -> Path:
    return Path(ctx.obj["out_dir"])


def _require_graph(ctx: click.Context):
    """Load the query engine; exit gracefully if graph.json is missing."""
    from springmap.query.engine import QueryEngine
    out = _out_path(ctx)
    engine = QueryEngine(out)
    try:
        engine._load()
    except FileNotFoundError as exc:
        err_console.print(f"[bold red]✗[/bold red] {exc}")
        sys.exit(1)
    return engine


# ─────────────────────────────────────────────
# CLI group
# ─────────────────────────────────────────────

@click.group()
@click.option(
    "--out",
    "out_dir",
    default="./springmap-out",
    show_default=True,
    help="Output directory for graph.json, GRAPH.md, manifest.json",
    type=click.Path(),
)
@click.pass_context
def main(ctx: click.Context, out_dir: str) -> None:
    """
    \b
    SpringMap — Knowledge graph builder for Spring Boot projects.

    \b
    Workflow:
      springmap build ./my-project      # first time
      springmap update ./my-project     # after code changes
      springmap query "user auth"       # search without re-parsing
      springmap show UserService        # class details
      springmap endpoints --method POST # list endpoints
    """
    ctx.ensure_object(dict)
    ctx.obj["out_dir"] = out_dir


# ─────────────────────────────────────────────
# build
# ─────────────────────────────────────────────

@main.command()
@click.argument("project_root", default=".", type=click.Path(exists=True))
@click.option("--quiet", "-q", is_flag=True, help="Suppress progress output")
@click.pass_context
def build(ctx: click.Context, project_root: str, quiet: bool) -> None:
    """Build the full knowledge graph from scratch."""
    from springmap.exporters.json_exporter import export_json
    from springmap.exporters.markdown_exporter import export_markdown
    from springmap.graph.builder import build_graph

    out = _out_path(ctx)
    project_root = str(Path(project_root).resolve())

    if not quiet:
        console.print(
            Panel(
                f"[bold]Project:[/bold] {project_root}\n"
                f"[bold]Output:[/bold]  {out}",
                title="🗺️  SpringMap Build",
                border_style="bright_blue",
            )
        )

    t0 = time.time()
    graph = build_graph(project_root, out)

    json_path = export_json(graph, out)
    md_path = export_markdown(graph, out)

    elapsed = time.time() - t0

    if not quiet:
        # Summary table
        tbl = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
        tbl.add_column("", style="dim")
        tbl.add_column("")

        counts = {
            "Controllers": len(graph.controllers),
            "Services": len(graph.services),
            "Repositories": len(graph.repositories),
            "Entities": len(graph.entities),
            "Other classes": len(graph.classes) - len(graph.controllers)
                            - len(graph.services) - len(graph.repositories)
                            - len(graph.entities),
            "REST endpoints": len(graph.all_endpoints),
        }
        grpc_count = len(graph.grpc_services)
        listener_count = len(graph.all_listeners)
        if grpc_count:
            counts["gRPC RPCs"] = grpc_count
        if listener_count:
            counts["Event listeners"] = listener_count
        for k, v in counts.items():
            tbl.add_row(k, str(v))

        console.print(tbl)
        console.print(
            f"[dim]📄[/dim] GRAPH.md   → [link={md_path}]{md_path}[/link] "
            f"[dim]({md_path.stat().st_size // 1024} KB)[/dim]"
        )
        console.print(
            f"[dim]📊[/dim] graph.json → [link={json_path}]{json_path}[/link] "
            f"[dim]({json_path.stat().st_size // 1024} KB)[/dim]"
        )
        console.print(f"\n[bold green]✓[/bold green] Built in [bold]{elapsed:.1f}s[/bold]")


# ─────────────────────────────────────────────
# update
# ─────────────────────────────────────────────

@main.command()
@click.argument("project_root", default=".", type=click.Path(exists=True))
@click.pass_context
def update(ctx: click.Context, project_root: str) -> None:
    """Incrementally re-parse only changed files."""
    from springmap.exporters.json_exporter import export_json, load_graph_json
    from springmap.exporters.markdown_exporter import export_markdown
    from springmap.graph.builder import build_graph, update_graph
    from springmap.graph.models import ProjectGraph

    out = _out_path(ctx)
    project_root = str(Path(project_root).resolve())

    existing_data = load_graph_json(out)
    if existing_data is None:
        console.print("[yellow]No existing graph found — running full build.[/yellow]")
        ctx.invoke(build, project_root=project_root)
        return

    # Reconstruct a lightweight ProjectGraph from JSON (only needs class names + metadata)
    from springmap.graph.builder import build_graph

    t0 = time.time()

    # We re-use build_graph's metadata parsing but merge with update_graph's incremental logic
    from springmap.graph.builder import update_graph
    from springmap.exporters.json_exporter import load_graph_json

    # Restore graph object from JSON
    existing = _json_to_graph(existing_data)
    updated, changed = update_graph(project_root, out, existing)

    if changed == 0:
        console.print("[green]✓[/green] Nothing changed — graph is up to date.")
        return

    export_json(updated, out)
    export_markdown(updated, out)
    console.print(
        f"[green]✓[/green] Updated [bold]{changed}[/bold] file(s) in "
        f"[bold]{time.time() - t0:.1f}s[/bold]"
    )


def _json_to_graph(data: dict):
    """Reconstruct a minimal ProjectGraph from graph.json for incremental updates."""
    from springmap.graph.models import (
        AnnotationInfo, ClassNode, FieldInfo, MethodInfo,
        NodeType, ParamInfo, ProjectConfig, ProjectGraph,
    )

    cfg_d = data.get("config", {})
    config = ProjectConfig(
        server_port=cfg_d.get("server_port", "8080"),
        context_path=cfg_d.get("context_path", ""),
        active_profiles=cfg_d.get("active_profiles", []),
        datasource_url=cfg_d.get("datasource_url"),
        datasource_driver=cfg_d.get("datasource_driver"),
        jpa_ddl_auto=cfg_d.get("jpa_ddl_auto"),
        jpa_show_sql=cfg_d.get("jpa_show_sql", False),
        custom_props=cfg_d.get("custom_props", {}),
    )

    graph = ProjectGraph(
        project_name=data.get("project_name", ""),
        base_package=data.get("base_package", ""),
        java_version=data.get("java_version", ""),
        spring_boot_version=data.get("spring_boot_version", ""),
        maven_dependencies=data.get("maven_dependencies", []),
        config=config,
        generated_at=data.get("generated_at", ""),
        source_root=data.get("source_root", ""),
    )

    # Rebuild classes (lightweight — enough for update_graph to work)
    for name, cls_d in data.get("classes", {}).items():
        methods = []
        for m in cls_d.get("methods", []):
            params = [ParamInfo(type=p["type"], name=p["name"]) for p in m.get("parameters", [])]
            methods.append(MethodInfo(
                name=m["name"],
                return_type=m.get("return_type", "void"),
                parameters=params,
                calls=m.get("calls", []),
                is_transactional=m.get("is_transactional", False),
                is_async=m.get("is_async", False),
                http_method=m.get("http_method"),
                http_path=m.get("http_path"),
                request_body_type=m.get("request_body_type"),
                line=m.get("line", 0),
            ))

        fields = []
        for f in cls_d.get("fields", []):
            fields.append(FieldInfo(
                name=f["name"],
                type=f["type"],
                is_injected=f.get("is_injected", False),
                is_id=f.get("is_id", False),
                column_name=f.get("column_name"),
                relationship=f.get("relationship"),
            ))

        try:
            nt = NodeType(cls_d.get("node_type", "unknown"))
        except ValueError:
            nt = NodeType.UNKNOWN

        node = ClassNode(
            name=name,
            package=cls_d.get("package", ""),
            file_path=cls_d.get("file_path", ""),
            node_type=nt,
            methods=methods,
            fields=fields,
            extends=cls_d.get("extends"),
            implements=cls_d.get("implements", []),
            table_name=cls_d.get("table_name"),
            base_path=cls_d.get("base_path"),
            is_interface=cls_d.get("is_interface", False),
            parse_error=cls_d.get("parse_error"),
            dependencies=cls_d.get("dependencies", []),
            dependents=cls_d.get("dependents", []),
            source=cls_d.get("source", "java"),
        )
        graph.classes[name] = node

    return graph


# ─────────────────────────────────────────────
# query
# ─────────────────────────────────────────────

@main.command()
@click.argument("query_text")
@click.option("--limit", default=25, show_default=True, help="Max results per category")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def query(ctx: click.Context, query_text: str, limit: int, as_json: bool) -> None:
    """
    Search the graph without re-parsing source files.

    \b
    Examples:
      springmap query "user auth"
      springmap query "type:service"
      springmap query "uses:UserRepository"
      springmap query "method:POST path:/api"
      springmap query "src:openapi"
    """
    engine = _require_graph(ctx)
    result = engine.search(query_text, limit=limit)

    if as_json:
        click.echo(json.dumps(
            {"classes": result.classes, "endpoints": [
                {"path": e["method"].get("http_path"), "method": e["method"].get("http_method"),
                 "controller": e["cls"]["name"]}
                for e in result.endpoints
            ]},
            indent=2, default=str,
        ))
        return

    if result.is_empty:
        console.print(f"[yellow]No results for:[/yellow] {query_text}")
        console.print("[dim]Tip: try  type:service  uses:ClassName  path:/api  method:GET[/dim]")
        return

    console.print(
        Panel(
            f'[bold]Query:[/bold] "{query_text}"  '
            f'[dim]│[/dim]  [bold]{result.total}[/bold] match(es)',
            border_style="dim",
        )
    )

    # Endpoints
    if result.endpoints:
        console.print("\n[bold underline]Endpoints[/bold underline]")
        tbl = Table(show_header=True, box=box.SIMPLE, padding=(0, 1))
        tbl.add_column("Method", width=8)
        tbl.add_column("Path", style="bright_cyan")
        tbl.add_column("Handler")
        tbl.add_column("File", style="dim")
        for ep in result.endpoints:
            m = ep["method"]
            cls = ep["cls"]
            tbl.add_row(
                _method_badge(m.get("http_method", "")),
                m.get("http_path") or "/",
                f"{cls['name']}.{m['name']}()",
                cls.get("file_path", ""),
            )
        console.print(tbl)

    # Classes grouped by type
    if result.classes:
        by_type: dict[str, list[dict]] = {}
        for cls in result.classes:
            by_type.setdefault(cls.get("node_type", "unknown"), []).append(cls)

        for node_type, nodes in sorted(by_type.items()):
            _, label = TYPE_STYLE.get(node_type, ("", node_type.title()))
            console.print(f"\n[bold underline]{label}s ({len(nodes)})[/bold underline]")
            for cls in nodes:
                _print_class_summary(cls)


def _print_class_summary(cls: dict) -> None:
    name = cls["name"]
    file_ = cls.get("file_path", "")
    deps = ", ".join(f"[bright_yellow]{d}[/bright_yellow]" for d in cls.get("dependencies", [])[:4])
    deps_line = f"  [dim]Injects:[/dim] {deps}" if deps else ""
    used_by = ", ".join(cls.get("dependents", [])[:4])
    used_line = f"  [dim]Used by:[/dim] {used_by}" if used_by else ""
    methods = cls.get("methods", [])
    ep_count = sum(1 for m in methods if m.get("http_method"))
    console.print(f"  [bold]{name}[/bold]  [dim]{file_}[/dim]")
    if deps_line:
        console.print(deps_line)
    if used_line:
        console.print(used_line)
    if ep_count:
        console.print(f"  [dim]Endpoints:[/dim] {ep_count}")


# ─────────────────────────────────────────────
# show
# ─────────────────────────────────────────────

@main.command()
@click.argument("class_name")
@click.pass_context
def show(ctx: click.Context, class_name: str) -> None:
    """Show full details for a class, service, entity, or controller."""
    engine = _require_graph(ctx)
    cls = engine.show_class(class_name)

    if cls is None:
        err_console.print(f"[red]Class '{class_name}' not found.[/red]")
        suggestions = engine.fuzzy_class_names(class_name)
        if suggestions:
            console.print(f"[dim]Did you mean:[/dim] {', '.join(suggestions)}")
        sys.exit(1)

    nt = cls.get("node_type", "unknown")
    _, label = TYPE_STYLE.get(nt, ("", nt.title()))

    console.print(Panel(
        f"[bold]{cls['name']}[/bold]  "
        + str(_type_badge(nt)),
        title="CLASS DETAILS", border_style="bright_blue",
    ))

    # Metadata grid
    tbl = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
    tbl.add_column("", style="dim", width=14)
    tbl.add_column("")

    tbl.add_row("File", f"[cyan]{cls.get('file_path', '')}[/cyan]")
    tbl.add_row("Package", cls.get("package", ""))
    if cls.get("extends"):
        tbl.add_row("Extends", cls["extends"])
    if cls.get("implements"):
        tbl.add_row("Implements", ", ".join(cls["implements"]))
    if cls.get("table_name"):
        tbl.add_row("Table", f"`{cls['table_name']}`")
    if cls.get("base_path"):
        tbl.add_row("Base path", f"`{cls['base_path']}`")
    if cls.get("source") != "java":
        tbl.add_row("Source", cls["source"])
    if cls.get("dependencies"):
        tbl.add_row("Injects", ", ".join(cls["dependencies"][:8]))
    if cls.get("dependents"):
        tbl.add_row("Used by", ", ".join(cls["dependents"][:8]))
    console.print(tbl)

    methods = cls.get("methods", [])

    # Endpoints — REST and gRPC (request/response style)
    from springmap.graph.models import categorize_verb
    endpoints = [m for m in methods if m.get("http_method") and categorize_verb(m["http_method"]) in ("rest", "grpc")]
    if endpoints:
        console.print("\n[bold]Endpoints[/bold]")
        ep_tbl = Table(box=box.SIMPLE, padding=(0, 1))
        ep_tbl.add_column("Method", width=8)
        ep_tbl.add_column("Path", style="bright_cyan")
        ep_tbl.add_column("Signature")
        ep_tbl.add_column("Body", style="dim")
        ep_tbl.add_column("Returns", style="dim")
        for m in endpoints:
            ep_tbl.add_row(
                _method_badge(m.get("http_method", "")),
                m.get("http_path") or "/",
                m.get("signature", ""),
                _short_type(m.get("request_body_type") or "—"),
                _short_type(m.get("return_type") or "void"),
            )
        console.print(ep_tbl)

    # Event listeners — Kafka/RabbitMQ/SQS/JMS/@EventListener/@Scheduled
    listeners = [m for m in methods if m.get("http_method") and categorize_verb(m["http_method"]) == "listener"]
    if listeners:
        console.print("\n[bold]Event Listeners[/bold]")
        lis_tbl = Table(box=box.SIMPLE, padding=(0, 1))
        lis_tbl.add_column("Type", width=10)
        lis_tbl.add_column("Topic / Cron", style="bright_cyan")
        lis_tbl.add_column("Handler")
        for m in listeners:
            lis_tbl.add_row(
                _method_badge(m.get("http_method", "")),
                m.get("http_path") or "—",
                m.get("signature", m.get("name", "")),
            )
        console.print(lis_tbl)

    # Other methods (no endpoint/listener annotation)
    other = [m for m in methods if not m.get("http_method")]
    if other:
        console.print("\n[bold]Methods[/bold]")
        m_tbl = Table(box=box.SIMPLE, padding=(0, 1))
        m_tbl.add_column("Signature")
        m_tbl.add_column("Calls", style="dim")
        m_tbl.add_column("Flags", style="dim", width=20)
        for m in other:
            flags: list[str] = []
            if m.get("is_transactional"):
                flags.append("@Tx")
            if m.get("is_async"):
                flags.append("@Async")
            if m.get("is_scheduled"):
                flags.append("@Scheduled")
            m_tbl.add_row(
                m.get("signature", m.get("name", "")),
                ", ".join(m.get("calls", [])[:3]) or "—",
                "  ".join(flags),
            )
        console.print(m_tbl)

    # Entity fields
    if nt == "entity":
        fields = cls.get("fields", [])
        if fields:
            console.print("\n[bold]Fields[/bold]")
            f_tbl = Table(box=box.SIMPLE, padding=(0, 1))
            f_tbl.add_column("Field")
            f_tbl.add_column("Type", style="cyan")
            f_tbl.add_column("Column", style="dim")
            f_tbl.add_column("Notes", style="dim")
            for f in fields:
                notes: list[str] = []
                if f.get("is_id"):
                    notes.append("PK")
                if f.get("relationship"):
                    notes.append(f"@{f['relationship']}")
                f_tbl.add_row(
                    f["name"],
                    f["type"],
                    f.get("column_name") or f["name"],
                    ", ".join(notes),
                )
            console.print(f_tbl)


# ─────────────────────────────────────────────
# path
# ─────────────────────────────────────────────

@main.command()
@click.argument("from_class")
@click.argument("to_class")
@click.pass_context
def path(ctx: click.Context, from_class: str, to_class: str) -> None:
    """Find shortest dependency path between two classes."""
    engine = _require_graph(ctx)
    result = engine.find_path(from_class, to_class)

    if not result.found:
        console.print(
            f"[yellow]No dependency path found between "
            f"[bold]{from_class}[/bold] and [bold]{to_class}[/bold].[/yellow]"
        )
        console.print(
            "[dim]They may be in unrelated domains, or the path exceeds graph depth.[/dim]"
        )
        sys.exit(1)

    console.print(
        Panel(
            f"[bold]{from_class}[/bold] [dim]→[/dim] [bold]{to_class}[/bold]  "
            f"[dim]({result.distance} hop{'s' if result.distance != 1 else ''})[/dim]",
            title="🔗 Dependency Path",
            border_style="bright_green",
        )
    )

    tree = Tree(f"[bold bright_blue]{result.hops[0]}[/bold bright_blue]")
    node = tree
    classes = engine.classes
    for i, hop in enumerate(result.hops[1:], 1):
        cls_data = classes.get(hop, {})
        nt = cls_data.get("node_type", "unknown")
        _, label = TYPE_STYLE.get(nt, ("", nt.title()))
        deps_via = classes.get(result.hops[i - 1], {}).get("dependencies", [])
        edge_label = "injects" if hop in deps_via else "depends on"
        branch = node.add(
            f"[dim]{edge_label} →[/dim] [bold]{hop}[/bold] "
            f"[dim]({label})[/dim]"
        )
        node = branch

    console.print(tree)


# ─────────────────────────────────────────────
# endpoints
# ─────────────────────────────────────────────

@main.command()
@click.argument("project_root", default=None, required=False, metavar="[PROJECT_ROOT]")
@click.option(
    "--method",
    default="",
    help="Filter by verb (GET/POST/.../KAFKA/RABBIT/...). Case-insensitive.",
)
@click.option("--filter", "path_filter", default="", help="Filter by path/topic substring")
@click.option("--grpc", "show_grpc", is_flag=True, help="Show gRPC RPCs only")
@click.option("--listeners", "--events", "show_listeners", is_flag=True,
              help="Show Kafka/RabbitMQ/SQS/JMS/@EventListener/@Scheduled only")
@click.option("--all", "show_all", is_flag=True, help="Show REST + gRPC + listeners together")
@click.pass_context
def endpoints(
    ctx: click.Context,
    project_root: str | None,
    method: str,
    path_filter: str,
    show_grpc: bool,
    show_listeners: bool,
    show_all: bool,
) -> None:
    """List REST endpoints (default), or gRPC / listener methods with flags.

    \b
    Examples:
      springmap endpoints                  # REST endpoints only (default)
      springmap endpoints .
      springmap endpoints --method POST
      springmap endpoints --filter /api/v1
      springmap endpoints --grpc           # gRPC RPCs only
      springmap endpoints --listeners      # Kafka/RabbitMQ/@Scheduled only
      springmap endpoints --all            # everything, one table
    """
    if project_root is not None and ctx.obj["out_dir"] == "./springmap-out":
        ctx.obj["out_dir"] = str(Path(project_root).resolve() / "springmap-out")
    engine = _require_graph(ctx)

    if sum([show_grpc, show_listeners, show_all]) > 1:
        err_console.print("[red]Use only one of --grpc / --listeners / --all at a time.[/red]")
        sys.exit(1)

    if show_grpc:
        kind, label = "grpc", "gRPC RPCs"
    elif show_listeners:
        kind, label = "listener", "Event Listeners & Scheduled Jobs"
    elif show_all:
        kind, label = "all", "All Endpoints (REST + gRPC + Listeners)"
    else:
        kind, label = "rest", "REST Endpoints"

    eps = engine.list_endpoints(method_filter=method, path_filter=path_filter, kind=kind)

    if not eps:
        console.print(f"[yellow]No {label.lower()} found with the given filters.[/yellow]")
        if kind == "rest":
            console.print(
                "[dim]Tip: gRPC and listener methods are hidden by default — "
                "try --grpc or --listeners.[/dim]"
            )
        return

    console.print(Panel(f"{label} ({len(eps)})", border_style="bright_blue"))

    tbl = Table(box=box.SIMPLE_HEAD, padding=(0, 1))
    tbl.add_column("Type", width=10, no_wrap=True)
    tbl.add_column("Path / Topic", style="bright_cyan")
    tbl.add_column("Class")
    tbl.add_column("Handler")
    tbl.add_column("Body", style="dim")
    tbl.add_column("Returns", style="dim")

    for ep in eps:
        tbl.add_row(
            _method_badge(ep["http_method"]),
            ep["path"],
            ep["controller"],
            ep["handler"] + "()",
            _short_type(ep.get("request_body") or "—"),
            _short_type(ep.get("return_type") or "void"),
        )

    console.print(tbl)


# ─────────────────────────────────────────────
# stats
# ─────────────────────────────────────────────

@main.command()
@click.argument("project_root", default=None, required=False, metavar="[PROJECT_ROOT]")
@click.pass_context
def stats(ctx: click.Context, project_root: str | None) -> None:
    """Graph statistics and parse quality report.

    \b
    Examples:
      springmap stats
      springmap stats .
    """
    if project_root is not None and ctx.obj["out_dir"] == "./springmap-out":
        ctx.obj["out_dir"] = str(Path(project_root).resolve() / "springmap-out")
    engine = _require_graph(ctx)
    s = engine.stats()

    console.print(Panel(
        f"[bold]{s.project_name}[/bold]  [dim]built at {s.generated_at}[/dim]",
        title="📊 SpringMap Statistics",
        border_style="bright_blue",
    ))

    # Class counts
    tbl = Table(show_header=True, box=box.SIMPLE, padding=(0, 2))
    tbl.add_column("Layer", style="bold")
    tbl.add_column("Count", justify="right")
    tbl.add_column("", style="dim")

    type_order = [
        "controller", "service", "repository", "entity",
        "component", "configuration", "dto", "exception", "util",
        "openapi", "grpc", "interface", "main", "unknown",
    ]
    for t in type_order:
        count = s.by_type.get(t, 0)
        if count == 0:
            continue
        _, label = TYPE_STYLE.get(t, ("", t.title()))
        tbl.add_row(label, str(count), "")

    tbl.add_section()
    tbl.add_row("[bold]Total classes[/bold]", str(s.total_classes), f"{s.total_methods} method(s)")

    console.print(tbl)

    # Endpoint breakdown — REST / gRPC / Listeners are tracked separately so
    # one category can never silently hide another (the bug this fixes).
    console.print("\n[bold]Endpoint Breakdown[/bold]")
    ep_tbl = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    ep_tbl.add_column("", style="dim")
    ep_tbl.add_column("", justify="right")
    ep_tbl.add_row("REST endpoints (controllers + OpenAPI)", str(s.rest_endpoints))
    if s.grpc_endpoints:
        ep_tbl.add_row("gRPC RPCs", str(s.grpc_endpoints))
    if s.listener_endpoints:
        ep_tbl.add_row("Event listeners / scheduled jobs", str(s.listener_endpoints))
    console.print(ep_tbl)

    # Parse quality
    console.print("\n[bold]Parse Quality[/bold]")
    q_tbl = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    q_tbl.add_column("", style="dim")
    q_tbl.add_column("", justify="right")
    q_tbl.add_row("AST parsed (javalang)", str(s.ast_parsed))
    if s.regex_fallback:
        q_tbl.add_row("[yellow]Regex fallback[/yellow]", str(s.regex_fallback))
    if s.openapi_nodes:
        q_tbl.add_row("From OpenAPI specs", str(s.openapi_nodes))
    if s.proto_nodes:
        q_tbl.add_row("From .proto files", str(s.proto_nodes))
    console.print(q_tbl)

    # File sizes
    console.print(
        f"\n[dim]graph.json[/dim] {s.graph_json_kb} KB  "
        f"[dim]GRAPH.md[/dim] {s.graph_md_kb} KB"
    )


# ─────────────────────────────────────────────
# clean
# ─────────────────────────────────────────────

@main.command()
@click.argument("project_root", default=None, required=False, metavar="[PROJECT_ROOT]")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def clean(ctx: click.Context, project_root: str | None, yes: bool) -> None:
    """Delete the springmap-out/ directory.

    \b
    Examples:
      springmap clean                 # delete ./springmap-out
      springmap clean .               # same — project root is optional
      springmap clean /path/to/proj   # delete /path/to/proj/springmap-out
    """
    # If project root is given, compute out/ relative to it
    # (mirrors how build/update work).  --out always wins when set explicitly.
    if project_root is not None and ctx.obj["out_dir"] == "./springmap-out":
        out = Path(project_root).resolve() / "springmap-out"
    else:
        out = _out_path(ctx)

    if not out.exists():
        console.print(f"[dim]Nothing to clean — {out} does not exist.[/dim]")
        return
    if not yes:
        click.confirm(f"Delete {out}?", abort=True)
    shutil.rmtree(out)
    console.print(f"[green]✓[/green] Deleted {out}")


if __name__ == "__main__":
    main()