import json
import click
from pathlib import Path

class QueryEngine:
    def __init__(self, out_dir: str):
        self.json_path = Path(out_dir) / "graph.json"

    def search(self, query: str):
        if not self.json_path.exists():
            click.echo(click.style("Error: graph.json not found. Run 'build .' first.", fg="red"))
            return

        data = json.loads(self.json_path.read_text(encoding='utf-8'))
        classes = data.get("classes", {})
        
        click.echo(click.style(f"\n🔍 Searching SpringMap for: '{query}'", fg="cyan", bold=True))
        click.echo("=" * 60)
        
        # 1. Parse Smart Filters (e.g., "type:service tx")
        filters = {}
        terms = []
        for part in query.split():
            if ':' in part:
                key, value = part.split(':', 1)
                filters[key.lower()] = value.lower()
            else:
                terms.append(part.lower())
        
        search_term = " ".join(terms)
        results_found = 0

        for cls_name, cls_data in classes.items():
            node_type = cls_data.get('node_type', '').lower()
            
            # 2. Apply Filters
            if 'type' in filters and filters['type'] not in node_type:
                continue
            if 'uses' in filters:
                deps = [d.lower() for d in cls_data.get('dependencies', [])]
                if not any(filters['uses'] in d for d in deps):
                    continue

            matches = []
            
            # 3. Deep Text Search (if a search term exists)
            if search_term:
                # Class Name
                if search_term in cls_name.lower():
                    matches.append(click.style("Class Name Match", fg="magenta"))
                    
                # Endpoints (REST & gRPC)
                for ep in cls_data.get("endpoints", []):
                    ep_path = str(ep.get("http_path", "")).lower()
                    ep_name = str(ep.get("name", "")).lower()
                    if search_term in ep_path or search_term in ep_name:
                        method = ep.get('http_method', 'ANY')
                        path = ep.get('http_path', '/')
                        matches.append(f"Endpoint: [{method}] {path} ➔ {ep.get('name')}()")
                        
                # Internal Methods
                for m in cls_data.get("methods", []):
                    m_name = str(m.get("name", "")).lower()
                    if search_term in m_name:
                        flags = " [@Transactional]" if m.get("is_transactional") else ""
                        matches.append(f"Method: {m.get('signature')}{flags}")

            # 4. Render the Result (If filters matched, or text matched)
            if (filters and not search_term) or matches:
                results_found += 1
                click.echo(click.style(f"📦 {cls_name} ", fg="green", bold=True) + click.style(f"[{node_type.upper()}]", fg="yellow"))
                
                # Show matches
                if matches:
                    for match in matches[:5]: # Limit to 5 so terminal doesn't flood
                        click.echo(f"   ↳ {match}")
                    if len(matches) > 5:
                        click.echo(click.style(f"   ↳ ... and {len(matches) - 5} more method matches.", dim=True))
                        
                # Show Architecture Context
                deps = cls_data.get('dependencies', [])
                if deps:
                    click.echo(f"   🔗 Injects: {', '.join(deps[:4])}" + ("..." if len(deps)>4 else ""))
                
                click.echo("-" * 60)

        # 5. Summary
        if results_found == 0:
            click.echo(click.style("No results found. Try adjusting your search terms.", fg="red"))
        else:
            click.echo(click.style(f"\n✅ Found {results_found} matching components.", fg="green", bold=True))