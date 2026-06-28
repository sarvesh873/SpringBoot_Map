import click
import json
import dataclasses
import shutil
from enum import Enum
from pathlib import Path
from collections import deque
from springmap.graph.builder import GraphBuilder
from springmap.query.engine import QueryEngine
from springmap.exporters.markdown_exporter import export_markdown

class SpringMapEncoder(json.JSONEncoder):
    def default(self, obj):
        if dataclasses.is_dataclass(obj): return dataclasses.asdict(obj)
        if isinstance(obj, Enum): return obj.value
        return super().default(obj)

def _load_graph(project_path: str) -> dict:
    json_path = Path(project_path) / "springmap-out" / "graph.json"
    if not json_path.exists():
        click.echo(click.style(f"Error: graph.json not found at {json_path.absolute()}. Please run 'build .' first.", fg="red"))
        raise click.Abort()
    with open(json_path, 'r', encoding='utf-8') as f:
        return json.load(f)

@click.group()
def cli():
    """SpringMap - Local Codebase Mapper for AI Optimization"""
    pass

@cli.command()
@click.argument('path', default='.')
def build(path):
    """Full extraction and graph generation."""
    click.echo(f"Building SpringMap for project at: {Path(path).absolute()}...")
    builder = GraphBuilder(path)
    graph = builder.build()
    
    out_dir = Path(path) / "springmap-out"
    out_dir.mkdir(exist_ok=True)
    
    with open(out_dir / "graph.json", 'w', encoding='utf-8') as f:
        json.dump(graph, f, cls=SpringMapEncoder, indent=2)
    export_markdown(graph, out_dir)
    click.echo(click.style(f"Success! Generated GRAPH.md and graph.json in {out_dir}", fg="green"))

@cli.command()
@click.argument('path', default='.')
def update(path):
    """Only re-extract changed files."""
    click.echo("Running full build...")
    build.callback(path)

@cli.command()
@click.argument('question')
@click.argument('path', default='.')
def query(question, path):
    """Search the generated graph JSON."""
    out_dir = Path(path) / "springmap-out"
    engine = QueryEngine(out_dir)
    engine.search(question)

@cli.command()
@click.argument('class_name')
@click.argument('path', default='.')
def show(class_name, path):
    """Show full details for a specific class."""
    data = _load_graph(path)
    classes = data.get("classes", {})
    if class_name not in classes:
        click.echo(click.style(f"Class '{class_name}' not found in graph.", fg="yellow"))
        return
    
    cls_data = classes[class_name]
    click.echo(f"\n--- {cls_data['name']} ({cls_data['node_type'].upper()}) ---")
    click.echo(f"File: {cls_data['file_path']}")
    click.echo(f"Injects: {', '.join(cls_data['dependencies']) if cls_data['dependencies'] else 'None'}")
    click.echo(f"Used By: {', '.join(cls_data['dependents']) if cls_data['dependents'] else 'None'}")

@cli.command()
@click.argument('start_class')
@click.argument('end_class')
@click.argument('path', default='.')
def path(start_class, end_class, path):
    """Find the shortest dependency path between two classes."""
    data = _load_graph(path)
    classes = data.get("classes", {})
    
    if start_class not in classes or end_class not in classes:
        click.echo(click.style("One or both classes not found in the graph.", fg="yellow"))
        return

    queue = deque([[start_class]])
    visited = set([start_class])
    
    while queue:
        current_path = queue.popleft()
        node = current_path[-1]
        
        if node == end_class:
            click.echo(f"\nShortest Path: {' ➔ '.join(current_path)}")
            return
            
        for neighbor in classes.get(node, {}).get("dependencies", []):
            if neighbor in classes and neighbor not in visited:
                visited.add(neighbor)
                queue.append(current_path + [neighbor])
                
    click.echo(click.style(f"No direct dependency path found between {start_class} and {end_class}.", fg="yellow"))

@cli.command()
@click.argument('path', default='.')
def endpoints(path):
    """List all REST endpoints."""
    data = _load_graph(path)
    click.echo("\n--- REST Endpoints ---")
    found = False
    for cls_name, cls_data in data.get("classes", {}).items():
        for ep in cls_data.get("endpoints", []):
            found = True
            method = ep.get('http_method', 'ANY')
            route = ep.get('http_path', '/')
            click.echo(f"[{method}] {route} ➔ {cls_name}.{ep['name']}()")
    if not found:
        click.echo("No endpoints discovered.")

@cli.command()
@click.argument('path', default='.')
def stats(path):
    """Show node and edge counts."""
    data = _load_graph(path)
    classes = data.get("classes", {})
    
    total_classes = len(classes)
    total_deps = sum(len(c.get("dependencies", [])) for c in classes.values())
    total_endpoints = sum(len(c.get("endpoints", [])) for c in classes.values())
    
    click.echo("\n--- Codebase Statistics ---")
    click.echo(f"Total Components Scanned : {total_classes}")
    click.echo(f"Total Dependency Edges   : {total_deps}")
    click.echo(f"Total REST Endpoints     : {total_endpoints}")

@cli.command()
@click.argument('path', default='.')
def clean(path):
    """Wipe the springmap-out directory."""
    out_dir = Path(path) / "springmap-out"
    if out_dir.exists():
        shutil.rmtree(out_dir)
        click.echo(click.style(f"Cleaned: Removed {out_dir}", fg="yellow"))
    else:
        click.echo("Nothing to clean.")

if __name__ == '__main__':
    cli()