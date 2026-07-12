"""
HTML graph exporter — generates a self-contained interactive graph.html.

Uses D3.js v7 force-directed layout loaded from cdnjs.cloudflare.com.
All graph data is embedded as inline JSON — no server needed, fully offline
after first load. Opens in any modern browser.

Visual encoding:
  controller  → blue circle
  service     → green rounded-rect
  repository  → amber diamond
  entity      → purple hexagon
  component   → orange circle (dashed, for Kafka consumers etc.)
  grpc        → magenta circle
  openapi     → blue circle (dashed)
  config/util → gray small circle
  other       → gray circle

Edge encoding:
  dependency (DI)  → solid gray arrow
  kafka/listener   → dashed orange arrow + topic label
  grpc rpc         → dashed magenta arrow
  openapi          → dashed blue arrow
"""
from __future__ import annotations

import json
from pathlib import Path

from springmap.graph.models import NodeType, ProjectGraph, HTTP_VERBS, LISTENER_VERBS

# ── Node visual config per type ──────────────────────────────────────────────

_NODE_CONFIG: dict[str, dict] = {
    "controller":    {"shape": "circle",   "color": "#378ADD", "border": "#185FA5", "r": 26},
    "service":       {"shape": "rect",     "color": "#639922", "border": "#3B6D11", "r": 22},
    "repository":    {"shape": "diamond",  "color": "#BA7517", "border": "#854F0B", "r": 22},
    "entity":        {"shape": "hexagon",  "color": "#7F77DD", "border": "#534AB7", "r": 22},
    "component":     {"shape": "circle",   "color": "#E07A20", "border": "#B05510", "r": 20, "dash": True},
    "grpc":          {"shape": "circle",   "color": "#D4537E", "border": "#993556", "r": 22, "dash": True},
    "openapi":       {"shape": "circle",   "color": "#378ADD", "border": "#185FA5", "r": 20, "dash": True},
    "configuration": {"shape": "circle",   "color": "#888780", "border": "#5F5E5A", "r": 16},
    "exception":     {"shape": "circle",   "color": "#E24B4A", "border": "#A32D2D", "r": 16},
    "dto":           {"shape": "circle",   "color": "#B4B2A9", "border": "#888780", "r": 14},
    "util":          {"shape": "circle",   "color": "#B4B2A9", "border": "#888780", "r": 14},
    "interface":     {"shape": "circle",   "color": "#AFA9EC", "border": "#7F77DD", "r": 16, "dash": True},
    "unknown":       {"shape": "circle",   "color": "#D3D1C7", "border": "#B4B2A9", "r": 14},
}

# Vertical clustering target (0=top, 1=bottom) — layers the graph into Spring tiers
_LAYER_Y: dict[str, float] = {
    "controller": 0.12,
    "openapi":    0.12,
    "grpc":       0.12,
    "service":    0.38,
    "component":  0.55,
    "repository": 0.68,
    "entity":     0.85,
}


def _build_graph_data(graph: ProjectGraph) -> dict:
    """Convert ProjectGraph into a D3-ready {nodes, edges} dict."""

    nodes = []
    edges = []
    class_names = set(graph.classes.keys())

    for cls in graph.classes.values():
        cfg = _NODE_CONFIG.get(cls.node_type.value, _NODE_CONFIG["unknown"])

        # Collect endpoint summary for the detail panel
        rest_eps = [
            f"{m.http_method} {m.http_path}"
            for m in cls.methods
            if m.http_method and m.http_method.upper() in HTTP_VERBS
        ]
        listeners = [
            f"{m.http_method} {m.http_path or '—'}"
            for m in cls.methods
            if m.http_method and m.http_method.upper() in LISTENER_VERBS
        ]
        method_names = [m.name for m in cls.methods if not m.http_method][:8]

        nodes.append({
            "id":          cls.name,
            "type":        cls.node_type.value,
            "file":        cls.file_path,
            "package":     cls.package,
            "source":      cls.source,
            "color":       cfg["color"],
            "border":      cfg["border"],
            "shape":       cfg["shape"],
            "r":           cfg["r"],
            "dash":        cfg.get("dash", False),
            "layer_y":     _LAYER_Y.get(cls.node_type.value, 0.5),
            "deps":        cls.dependencies[:12],
            "dependents":  cls.dependents[:8],
            "rest_eps":    rest_eps[:6],
            "listeners":   listeners[:6],
            "methods":     method_names,
            "base_path":   cls.base_path or "",
            "table_name":  cls.table_name or "",
        })

        # DI dependency edges
        for dep_name in cls.dependencies:
            if dep_name in class_names:
                edges.append({
                    "source": cls.name,
                    "target": dep_name,
                    "type":   "di",
                })

        # Listener method edges (Kafka topics → conceptual target not known,
        # but we emit self-referential metadata for the tooltip)
        for m in cls.methods:
            if m.http_method and m.http_method.upper() in LISTENER_VERBS:
                edges.append({
                    "source":  cls.name,
                    "target":  cls.name,  # self-loop — rendered as a badge, not an arrow
                    "type":    "listener",
                    "label":   f"{m.http_method}: {m.http_path or ''}",
                })

    # Deduplicate DI edges (both sides may have listed the same relationship)
    seen_edges: set[tuple] = set()
    deduped: list[dict] = []
    for e in edges:
        if e["type"] == "listener":
            deduped.append(e)
            continue
        key = (e["source"], e["target"])
        if key not in seen_edges:
            seen_edges.add(key)
            deduped.append(e)

    return {
        "nodes": nodes,
        "edges": deduped,
        "meta": {
            "project_name":        graph.project_name,
            "spring_boot_version": graph.spring_boot_version,
            "java_version":        graph.java_version,
            "generated_at":        graph.generated_at,
            "server_port":         graph.config.server_port,
            "datasource_url":      graph.config.datasource_url or "",
        }
    }


def export_html_graph(graph: ProjectGraph, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "graph.html"
    data = _build_graph_data(graph)
    graph_json = json.dumps(data, indent=None, separators=(",", ":"))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{graph.project_name} — SpringMap Graph</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f0f;color:#e2e0d8;overflow:hidden;height:100vh;display:flex;flex-direction:column}}
#toolbar{{height:50px;background:#1a1a1a;border-bottom:1px solid #2a2a2a;display:flex;align-items:center;gap:12px;padding:0 16px;flex-shrink:0}}
#toolbar h1{{font-size:13px;font-weight:500;color:#e2e0d8;white-space:nowrap;margin-right:8px}}
#toolbar span{{font-size:11px;color:#888;white-space:nowrap}}
#search{{padding:5px 10px;border-radius:6px;border:1px solid #333;background:#111;color:#e2e0d8;font-size:12px;width:200px;outline:none}}
#search:focus{{border-color:#555}}
.filter-btn{{padding:4px 10px;border-radius:5px;border:1px solid #333;background:#1a1a1a;color:#888;font-size:11px;cursor:pointer;transition:all .15s;white-space:nowrap}}
.filter-btn.active{{background:#333;color:#e2e0d8;border-color:#555}}
#sep{{width:1px;height:22px;background:#2a2a2a;margin:0 4px}}
.toggle-btn{{padding:4px 10px;border-radius:5px;border:1px solid #333;background:#1a1a1a;color:#666;font-size:11px;cursor:pointer;transition:all .15s}}
.toggle-btn.on{{color:#e2e0d8;border-color:#444}}
#main{{flex:1;display:flex;overflow:hidden}}
#canvas{{flex:1;position:relative;overflow:hidden}}
svg{{width:100%;height:100%}}
.link{{fill:none;stroke-width:1.5;opacity:0.6}}
.link.di{{stroke:#5a5a5a}}
.link.listener{{stroke:#E07A20;stroke-dasharray:5,4}}
.link.grpc{{stroke:#D4537E;stroke-dasharray:5,4}}
.node-group{{cursor:pointer}}
.node-group:hover .node-shape{{filter:brightness(1.25)}}
.node-label{{font-size:10px;fill:#c2c0b6;text-anchor:middle;pointer-events:none;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
.node-label.hidden{{display:none}}
#panel{{width:280px;background:#1a1a1a;border-left:1px solid #2a2a2a;padding:16px;overflow-y:auto;flex-shrink:0;display:none}}
#panel.visible{{display:block}}
#panel h2{{font-size:14px;font-weight:500;margin-bottom:4px;color:#e2e0d8}}
#panel .type-badge{{display:inline-block;font-size:10px;padding:2px 7px;border-radius:10px;margin-bottom:10px;font-weight:500}}
#panel .row{{margin-bottom:8px}}
#panel .row-label{{font-size:10px;color:#666;text-transform:uppercase;letter-spacing:.04em;margin-bottom:2px}}
#panel .row-val{{font-size:11px;color:#b0ae9f;line-height:1.5}}
#panel .ep{{display:flex;gap:6px;align-items:center;margin-bottom:3px}}
#panel .verb{{font-size:10px;font-weight:600;padding:1px 5px;border-radius:3px;min-width:42px;text-align:center}}
#panel .verb.GET{{background:#1a3a2a;color:#4ade80}}
#panel .verb.POST{{background:#1a2a3a;color:#60a5fa}}
#panel .verb.PUT{{background:#3a3a1a;color:#facc15}}
#panel .verb.DELETE{{background:#3a1a1a;color:#f87171}}
#panel .verb.PATCH{{background:#1a3a3a;color:#67e8f9}}
#panel .verb.KAFKA{{background:#3a2a1a;color:#fb923c}}
#panel .verb.RABBIT{{background:#3a2a1a;color:#fb923c}}
#panel .verb.SCHEDULED{{background:#2a2a2a;color:#9ca3af}}
#panel .close-btn{{float:right;cursor:pointer;color:#555;font-size:16px;margin-top:-2px}}
#panel .close-btn:hover{{color:#999}}
#legend{{position:absolute;bottom:16px;left:16px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;padding:10px 14px;font-size:10px;color:#777}}
#legend .leg-row{{display:flex;align-items:center;gap:6px;margin-bottom:4px}}
#legend .leg-row:last-child{{margin-bottom:0}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.leg-rect{{width:10px;height:8px;border-radius:2px;flex-shrink:0}}
.leg-dia{{width:10px;height:10px;transform:rotate(45deg);flex-shrink:0}}
#tooltip{{position:absolute;background:#222;border:1px solid #333;border-radius:6px;padding:6px 10px;font-size:11px;color:#e2e0d8;pointer-events:none;display:none;max-width:200px;z-index:10}}
</style>
</head>
<body>
<div id="toolbar">
  <h1>{graph.project_name}</h1>
  <span>Spring Boot {graph.spring_boot_version} · Java {graph.java_version} · Port {graph.config.server_port}</span>
  <div id="sep"></div>
  <input id="search" type="text" placeholder="Search classes…">
  <button class="filter-btn active" data-type="all">All</button>
  <button class="filter-btn" data-type="controller">Controllers</button>
  <button class="filter-btn" data-type="service">Services</button>
  <button class="filter-btn" data-type="repository">Repositories</button>
  <button class="filter-btn" data-type="entity">Entities</button>
  <div id="sep"></div>
  <button class="toggle-btn on" id="toggle-labels">Labels</button>
  <button class="toggle-btn on" id="toggle-di">DI edges</button>
  <button class="toggle-btn" id="toggle-events">Event edges</button>
</div>
<div id="main">
  <div id="canvas">
    <svg id="svg"></svg>
    <div id="legend">
      <div class="leg-row"><div class="leg-dot" style="background:#378ADD"></div>Controller / OpenAPI</div>
      <div class="leg-row"><div class="leg-rect" style="background:#639922"></div>Service</div>
      <div class="leg-row"><div class="leg-dia" style="background:#BA7517"></div>Repository</div>
      <div class="leg-row"><div class="leg-dot" style="background:#7F77DD"></div>Entity</div>
      <div class="leg-row"><div class="leg-dot" style="background:#E07A20;border:1px dashed #aaa"></div>Consumer / Component</div>
      <div class="leg-row"><div class="leg-dot" style="background:#D4537E;border:1px dashed #aaa"></div>gRPC</div>
      <div class="leg-row"><div style="width:20px;height:1.5px;background:#5a5a5a;flex-shrink:0"></div>DI injection</div>
      <div class="leg-row"><div style="width:20px;height:1.5px;background:#E07A20;border-top:1px dashed #E07A20;flex-shrink:0"></div>Event listener</div>
    </div>
    <div id="tooltip"></div>
  </div>
  <div id="panel">
    <span class="close-btn" id="close-panel">✕</span>
    <h2 id="p-name"></h2>
    <span class="type-badge" id="p-badge"></span>
    <div class="row"><div class="row-label">File</div><div class="row-val" id="p-file"></div></div>
    <div class="row"><div class="row-label">Package</div><div class="row-val" id="p-pkg"></div></div>
    <div class="row" id="p-path-row" style="display:none"><div class="row-label">Base path</div><div class="row-val" id="p-path"></div></div>
    <div class="row" id="p-table-row" style="display:none"><div class="row-label">Table</div><div class="row-val" id="p-table"></div></div>
    <div class="row" id="p-deps-row" style="display:none"><div class="row-label">Injects</div><div class="row-val" id="p-deps"></div></div>
    <div class="row" id="p-used-row" style="display:none"><div class="row-label">Used by</div><div class="row-val" id="p-used"></div></div>
    <div class="row" id="p-ep-row" style="display:none"><div class="row-label">REST Endpoints</div><div id="p-eps"></div></div>
    <div class="row" id="p-lis-row" style="display:none"><div class="row-label">Listeners</div><div id="p-lis"></div></div>
    <div class="row" id="p-meth-row" style="display:none"><div class="row-label">Methods</div><div class="row-val" id="p-meths"></div></div>
  </div>
</div>
<script>
const DATA = {graph_json};
const COLOR_MAP = {{
  controller:"#378ADD",service:"#639922",repository:"#BA7517",
  entity:"#7F77DD",component:"#E07A20",grpc:"#D4537E",openapi:"#378ADD",
  configuration:"#888780",exception:"#E24B4A",dto:"#B4B2A9",
  util:"#B4B2A9",interface:"#AFA9EC",unknown:"#D3D1C7"
}};
const BADGE_COLOR = {{
  controller:"#1a2a3a",service:"#1a2a1a",repository:"#2a2010",
  entity:"#221a3a",component:"#2a1a0a",grpc:"#2a0a1a",openapi:"#1a2a3a",
  configuration:"#1a1a1a",exception:"#2a0a0a",
}};

let showLabels=true, showDI=true, showEvents=false;
let activeFilter="all", searchTerm="", selectedNode=null;

const svg=d3.select("#svg");
const width=()=>document.getElementById("canvas").clientWidth;
const height=()=>document.getElementById("canvas").clientHeight;

const g=svg.append("g");
svg.call(d3.zoom().scaleExtent([0.2,3]).on("zoom",e=>g.attr("transform",e.transform)));

const nodes=DATA.nodes.map(d=>({{...d}}));
const edges=DATA.edges.filter(e=>e.source!==e.target).map(d=>({{...d}}));

const sim=d3.forceSimulation(nodes)
  .force("link",d3.forceLink(edges).id(d=>d.id).distance(140).strength(0.4))
  .force("charge",d3.forceManyBody().strength(-500))
  .force("collide",d3.forceCollide().radius(d=>(d.r||22)+12))
  .force("center",d3.forceCenter(0,0))
  .force("cluster",alpha=>{{
    nodes.forEach(n=>{{
      const ty=(n.layer_y||0.5)*height()-height()/2;
      n.vy+=(ty-n.y)*alpha*0.07;
    }});
  }});

const link=g.append("g").selectAll("line").data(edges).join("line")
  .attr("class",d=>`link ${{d.type}}`)
  .attr("marker-end","url(#arrow)");

const defs=svg.append("defs");
defs.append("marker").attr("id","arrow")
  .attr("viewBox","0 0 10 10").attr("refX",8).attr("refY",5)
  .attr("markerWidth",5).attr("markerHeight",5).attr("orient","auto-start-reverse")
  .append("path").attr("d","M2 1L8 5L2 9").attr("fill","none")
  .attr("stroke","#5a5a5a").attr("stroke-width",1.5).attr("stroke-linecap","round");

const nodeGroup=g.append("g").selectAll("g").data(nodes).join("g")
  .attr("class","node-group")
  .call(d3.drag()
    .on("start",(e,d)=>{{if(!e.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;}})
    .on("drag",(e,d)=>{{d.fx=e.x;d.fy=e.y;}})
    .on("end",(e,d)=>{{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}}))
  .on("click",(e,d)=>{{e.stopPropagation();selectNode(d);}})
  .on("mouseover",(e,d)=>{{
    if(selectedNode&&selectedNode.id!==d.id)return;
    const tt=document.getElementById("tooltip");
    tt.style.display="block";
    tt.style.left=(e.offsetX+12)+"px";
    tt.style.top=(e.offsetY-8)+"px";
    tt.textContent=d.id+(d.type?" ("+d.type+")":"");
  }})
  .on("mousemove",(e)=>{{
    document.getElementById("tooltip").style.left=(e.offsetX+12)+"px";
    document.getElementById("tooltip").style.top=(e.offsetY-8)+"px";
  }})
  .on("mouseleave",()=>{{document.getElementById("tooltip").style.display="none";}});

function drawShape(sel){{
  sel.each(function(d){{
    const el=d3.select(this);
    const r=d.r||22, dash=d.dash?"5,4":null;
    const col=d.color||"#888", brd=d.border||"#555";
    if(d.shape==="rect"){{
      el.append("rect").attr("class","node-shape")
        .attr("x",-r).attr("y",-(r*0.75)).attr("width",r*2).attr("height",r*1.5)
        .attr("rx",6).attr("fill",col).attr("stroke",brd).attr("stroke-width",1.5)
        .attr("stroke-dasharray",dash);
    }} else if(d.shape==="diamond"){{
      const s=r*1.2;
      el.append("polygon").attr("class","node-shape")
        .attr("points",`0,-${{s}} ${{s}},0 0,${{s}} -${{s}},0`)
        .attr("fill",col).attr("stroke",brd).attr("stroke-width",1.5)
        .attr("stroke-dasharray",dash);
    }} else if(d.shape==="hexagon"){{
      const pts=[0,1,2,3,4,5].map(i=>{{
        const a=Math.PI/180*(60*i-30);
        return [r*Math.cos(a),r*Math.sin(a)].join(",");
      }}).join(" ");
      el.append("polygon").attr("class","node-shape")
        .attr("points",pts).attr("fill",col).attr("stroke",brd).attr("stroke-width",1.5)
        .attr("stroke-dasharray",dash);
    }} else {{
      el.append("circle").attr("class","node-shape")
        .attr("r",r).attr("fill",col).attr("stroke",brd).attr("stroke-width",1.5)
        .attr("stroke-dasharray",dash);
    }}
    el.append("text").attr("class","node-label").attr("dy",r+13)
      .text(d.id.length>16?d.id.slice(0,15)+"…":d.id);
  }});
}}
nodeGroup.call(drawShape);

svg.on("click",()=>{{deselectNode();}});
document.getElementById("close-panel").onclick=deselectNode;

sim.on("tick",()=>{{
  link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y)
      .attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
  nodeGroup.attr("transform",d=>`translate(${{d.x}},${{d.y}})`);
}});

sim.on("end",()=>recenter());

function recenter(){{
  const W=width(),H=height();
  const xs=nodes.map(n=>n.x),ys=nodes.map(n=>n.y);
  const minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
  const cx=(minX+maxX)/2,cy=(minY+maxY)/2;
  const scaleX=W/(maxX-minX+200),scaleY=H/(maxY-minY+200);
  const scale=Math.min(scaleX,scaleY,1);
  svg.call(d3.zoom().transform,d3.zoomIdentity.translate(W/2-cx*scale,H/2-cy*scale).scale(scale));
}}

function selectNode(d){{
  selectedNode=d;
  document.getElementById("panel").classList.add("visible");
  document.getElementById("p-name").textContent=d.id;
  const badge=document.getElementById("p-badge");
  badge.textContent=d.type;
  badge.style.background=BADGE_COLOR[d.type]||"#1a1a1a";
  badge.style.color=COLOR_MAP[d.type]||"#aaa";
  document.getElementById("p-file").textContent=d.file||"—";
  document.getElementById("p-pkg").textContent=d.package||"—";
  const pp=document.getElementById("p-path-row"),pt=document.getElementById("p-table-row");
  if(d.base_path){{pp.style.display="";document.getElementById("p-path").textContent=d.base_path;}}else pp.style.display="none";
  if(d.table_name){{pt.style.display="";document.getElementById("p-table").textContent=d.table_name;}}else pt.style.display="none";
  const dRow=document.getElementById("p-deps-row"),uRow=document.getElementById("p-used-row");
  if(d.deps&&d.deps.length){{dRow.style.display="";document.getElementById("p-deps").textContent=d.deps.join(", ");}}else dRow.style.display="none";
  if(d.dependents&&d.dependents.length){{uRow.style.display="";document.getElementById("p-used").textContent=d.dependents.join(", ");}}else uRow.style.display="none";
  const epRow=document.getElementById("p-ep-row"),epsDiv=document.getElementById("p-eps");
  if(d.rest_eps&&d.rest_eps.length){{
    epRow.style.display="";
    epsDiv.innerHTML=d.rest_eps.map(ep=>{{
      const [verb,...rest]=ep.split(" ");
      return `<div class="ep"><span class="verb ${{verb}}">${{verb}}</span><span style="font-size:10px;color:#888">${{rest.join(" ")}}</span></div>`;
    }}).join("");
  }}else epRow.style.display="none";
  const lisRow=document.getElementById("p-lis-row"),lisDiv=document.getElementById("p-lis");
  if(d.listeners&&d.listeners.length){{
    lisRow.style.display="";
    lisDiv.innerHTML=d.listeners.map(l=>{{
      const [verb,...rest]=l.split(" ");
      return `<div class="ep"><span class="verb ${{verb}}">${{verb}}</span><span style="font-size:10px;color:#888">${{rest.join(" ")}}</span></div>`;
    }}).join("");
  }}else lisRow.style.display="none";
  const mRow=document.getElementById("p-meth-row");
  if(d.methods&&d.methods.length){{mRow.style.display="";document.getElementById("p-meths").textContent=d.methods.join(", ");}}else mRow.style.display="none";
  highlightNeighbours(d.id);
}}

function deselectNode(){{
  selectedNode=null;
  document.getElementById("panel").classList.remove("visible");
  nodeGroup.style("opacity",1);
  link.style("opacity",0.6);
}}

function highlightNeighbours(id){{
  const connected=new Set([id]);
  edges.forEach(e=>{{
    if(e.source.id===id)connected.add(e.target.id);
    if(e.target.id===id)connected.add(e.source.id);
  }});
  nodeGroup.style("opacity",d=>connected.has(d.id)?1:0.15);
  link.style("opacity",e=>{{
    const sid=typeof e.source==="object"?e.source.id:e.source;
    const tid=typeof e.target==="object"?e.target.id:e.target;
    return sid===id||tid===id?0.9:0.05;
  }});
}}

function applyFilters(){{
  nodeGroup.style("display",d=>{{
    if(activeFilter!=="all"&&d.type!==activeFilter)return"none";
    if(searchTerm&&!d.id.toLowerCase().includes(searchTerm))return"none";
    return null;
  }});
  link.style("display",e=>{{
    const sid=typeof e.source==="object"?e.source.id:e.source;
    const tid=typeof e.target==="object"?e.target.id:e.target;
    const sn=nodes.find(n=>n.id===sid),tn=nodes.find(n=>n.id===tid);
    if(!sn||!tn)return"none";
    if(activeFilter!=="all"&&sn.type!==activeFilter&&tn.type!==activeFilter)return"none";
    if(e.type==="di"&&!showDI)return"none";
    if((e.type==="listener"||e.type==="grpc")&&!showEvents)return"none";
    return null;
  }});
  nodeGroup.selectAll(".node-label").classed("hidden",!showLabels);
}}

document.querySelectorAll(".filter-btn").forEach(btn=>{{
  btn.onclick=()=>{{
    document.querySelectorAll(".filter-btn").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    activeFilter=btn.dataset.type;
    applyFilters();
  }};
}});
document.getElementById("search").oninput=e=>{{searchTerm=e.target.value.toLowerCase();applyFilters();}};
document.getElementById("toggle-labels").onclick=function(){{
  showLabels=!showLabels;this.classList.toggle("on",showLabels);applyFilters();
}};
document.getElementById("toggle-di").onclick=function(){{
  showDI=!showDI;this.classList.toggle("on",showDI);applyFilters();
}};
document.getElementById("toggle-events").onclick=function(){{
  showEvents=!showEvents;this.classList.toggle("on",showEvents);applyFilters();
}};

window.addEventListener("resize",()=>{{
  recenter();
}});
</script>
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")
    return out_path