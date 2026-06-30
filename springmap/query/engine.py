"""
Stateful query engine.

Reads graph.json once (cached in memory for the process lifetime) and answers
all query commands without touching any source files.

Filter syntax supported in query strings:
  type:service          — only service nodes
  type:controller       — only controllers
  uses:UserRepository   — classes that inject UserRepository
  used-by:UserCtrl      — classes that UserCtrl depends on
  path:/api/users       — endpoint path contains substring
  method:GET            — only GET endpoints
  kind:rest             — only REST endpoints (default for endpoints listing)
  kind:grpc             — only gRPC RPCs
  kind:listener         — only Kafka/RabbitMQ/SQS/JMS/EventListener/Scheduled
  pkg:com.example.svc   — package starts with prefix
  src:openapi           — nodes sourced from OpenAPI (not Java)
  src:proto             — nodes sourced from .proto files
  <keyword>             — matches name / file_path / package / method names / paths
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from springmap.exporters.json_exporter import load_graph_json
from springmap.graph.models import HTTP_VERBS, LISTENER_VERBS, RPC_VERB, categorize_verb


# ─────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────

@dataclass
class EndpointResult:
    http_method: str
    path: str
    controller: str
    handler: str
    signature: str
    request_body: Optional[str]
    return_type: str
    file: str


@dataclass
class SearchResult:
    query: str
    endpoints: list[dict] = field(default_factory=list)
    classes: list[dict] = field(default_factory=list)
    total: int = 0

    @property
    def is_empty(self) -> bool:
        return self.total == 0


@dataclass
class PathResult:
    found: bool
    hops: list[str] = field(default_factory=list)   # class names in path
    details: list[dict] = field(default_factory=list)  # ClassNode dicts for each hop

    @property
    def distance(self) -> int:
        return max(0, len(self.hops) - 1)


@dataclass
class StatsResult:
    project_name: str
    generated_at: str
    by_type: dict[str, int]
    total_classes: int
    total_methods: int
    total_endpoints: int        # REST only — kept for backward compatibility
    rest_endpoints: int
    grpc_endpoints: int
    listener_endpoints: int
    ast_parsed: int
    regex_fallback: int
    openapi_nodes: int
    proto_nodes: int
    graph_json_kb: float
    graph_md_kb: float


# ─────────────────────────────────────────────
# Query parser
# ─────────────────────────────────────────────

_FILTER_RE = re.compile(r"([\w-]+):([\S]+)")


def _parse_query(query: str) -> tuple[dict[str, str], str]:
    """
    Extract key:value filters from query string.
    Returns (filter_dict, remaining_keyword_text).
    """
    filters: dict[str, str] = {}
    remaining = query
    for m in _FILTER_RE.finditer(query):
        key = m.group(1).lower().replace("-", "_")  # used_by, used-by → used_by
        val = m.group(2)
        filters[key] = val
        remaining = remaining.replace(m.group(0), "").strip()
    return filters, remaining.strip()


# ─────────────────────────────────────────────
# QueryEngine
# ─────────────────────────────────────────────

class QueryEngine:
    """
    Load graph.json once; answer all query commands in memory.

    Usage:
        engine = QueryEngine(Path("./springmap-out"))
        result = engine.search("type:service user")
        details = engine.show_class("UserService")
        path = engine.find_path("UserController", "UserRepository")
    """

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self._data: Optional[dict] = None

    # ── Lazy loader ──────────────────────────

    def _load(self) -> dict:
        if self._data is None:
            data = load_graph_json(self.out_dir)
            if data is None:
                raise FileNotFoundError(
                    f"No graph.json found in '{self.out_dir}'.\n"
                    "Run  springmap build <project-root>  first."
                )
            self._data = data
        return self._data

    @property
    def classes(self) -> dict[str, dict]:
        return self._load().get("classes", {})

    # ── Class lookup ─────────────────────────

    def find_class(self, name: str) -> Optional[dict]:
        """Exact → case-insensitive → partial name match."""
        classes = self.classes

        if name in classes:
            return classes[name]

        lower = name.lower()
        # Case-insensitive exact match
        for k, v in classes.items():
            if k.lower() == lower:
                return v

        # Partial match — return only if unique
        partials = [v for k, v in classes.items() if lower in k.lower()]
        if len(partials) == 1:
            return partials[0]

        return None

    def fuzzy_class_names(self, name: str, limit: int = 5) -> list[str]:
        """Return class names containing `name` (case-insensitive)."""
        lower = name.lower()
        return [k for k in self.classes if lower in k.lower()][:limit]

    # ── Search ───────────────────────────────

    def search(self, query: str, limit: int = 25) -> SearchResult:
        """
        Keyword + filter search across the entire graph.

        Filters (may be combined with keywords):
          type:service / type:controller / type:repository / type:entity / …
          uses:<ClassName>      — classes that inject ClassName
          used_by:<ClassName>   — classes injected into ClassName
          path:/api/…           — endpoint path substring
          method:GET            — HTTP method
          pkg:com.example       — package prefix
          src:openapi / src:proto / src:java
        """
        filters, kw = _parse_query(query)
        data = self._load()
        classes = data.get("classes", {})

        matched_classes: list[dict] = []
        matched_endpoints: list[dict] = []
        kw_lower = kw.lower()

        for cls in classes.values():
            # ── Type filter ──
            if "type" in filters:
                if cls.get("node_type", "") != filters["type"].lower():
                    continue

            # ── Source filter ──
            if "src" in filters:
                if cls.get("source", "java") != filters["src"].lower():
                    continue

            # ── Package filter ──
            if "pkg" in filters:
                if not cls.get("package", "").startswith(filters["pkg"]):
                    continue

            # ── Dependency filter: uses:X means cls.dependencies contains X ──
            if "uses" in filters:
                target = filters["uses"].lower()
                deps = [d.lower() for d in cls.get("dependencies", [])]
                injected = [t.split("<")[0].lower() for t in cls.get("injected_types", [])]
                if target not in deps and target not in injected:
                    continue

            # ── Dependent filter: used_by:X means X is in cls.dependents ──
            if "used_by" in filters:
                caller = filters["used_by"].lower()
                if caller not in [d.lower() for d in cls.get("dependents", [])]:
                    continue

            # ── Keyword match ──
            if kw_lower:
                search_blob = " ".join([
                    cls.get("name", ""),
                    cls.get("package", ""),
                    cls.get("file_path", ""),
                    " ".join(m.get("name", "") for m in cls.get("methods", [])),
                    " ".join(
                        m.get("http_path", "") or ""
                        for m in cls.get("methods", [])
                        if m.get("http_method")
                    ),
                ]).lower()
                if kw_lower not in search_blob:
                    continue

            matched_classes.append(cls)

        # ── Endpoint-specific filters: path:, method:, kind: ──
        if "path" in filters or "method" in filters or "kind" in filters:
            requested_kind = filters.get("kind", "").lower()
            for cls in classes.values():
                for m in cls.get("methods", []):
                    hm = m.get("http_method")
                    if not hm:
                        continue
                    if "path" in filters and filters["path"] not in (m.get("http_path") or ""):
                        continue
                    if "method" in filters and hm.upper() != filters["method"].upper():
                        continue
                    if requested_kind and categorize_verb(hm) != requested_kind:
                        continue
                    matched_endpoints.append({"cls": cls, "method": m})

        return SearchResult(
            query=query,
            endpoints=matched_endpoints[:limit],
            classes=matched_classes[:limit],
            total=len(matched_classes) + len(matched_endpoints),
        )

    # ── Show ─────────────────────────────────

    def show_class(self, name: str) -> Optional[dict]:
        return self.find_class(name)

    # ── Path finding ─────────────────────────

    def find_path(self, from_class: str, to_class: str) -> PathResult:
        """
        BFS shortest dependency path from from_class to to_class.
        Traverses edges defined by .dependencies (resolved at build time).
        """
        classes = self.classes

        if from_class not in classes:
            return PathResult(found=False)
        if to_class not in classes:
            return PathResult(found=False)
        if from_class == to_class:
            return PathResult(found=True, hops=[from_class], details=[classes[from_class]])

        queue: deque[list[str]] = deque([[from_class]])
        visited: set[str] = {from_class}

        while queue:
            path = queue.popleft()
            current = path[-1]
            node = classes.get(current, {})

            neighbors = set(
                node.get("dependencies", [])
                + [t.split("<")[0].strip() for t in node.get("injected_types", [])]
            )

            for dep in neighbors:
                if dep in visited:
                    continue
                new_path = path + [dep]
                if dep == to_class:
                    details = [classes[n] for n in new_path if n in classes]
                    return PathResult(found=True, hops=new_path, details=details)
                if dep in classes:
                    visited.add(dep)
                    queue.append(new_path)

        return PathResult(found=False)

    # ── Endpoints ────────────────────────────

    def list_endpoints(
        self,
        method_filter: str = "",
        path_filter: str = "",
        type_filter: str = "",
        kind: str = "rest",
    ) -> list[dict]:
        """
        Return endpoints, filtered by category, HTTP/listener verb, path substring,
        or node type (controller / openapi / grpc / component).

        kind controls which CATEGORY of endpoint is returned:
          'rest'     (default) — only GET/POST/PUT/DELETE/PATCH/REQUEST
          'grpc'     — only gRPC RPC methods (verb shown as 'RPC')
          'listener' — only Kafka/RabbitMQ/SQS/JMS/@EventListener/@Scheduled
          'all'      — everything, regardless of category

        BUG FIX: previously this had no category concept at all, so calling
        list_endpoints() with no arguments silently returned BOTH REST and
        gRPC results mixed together — and on projects where REST endpoints
        failed to parse (a separate javalang bug, now fixed), only the gRPC
        rows were visible, making it look like REST endpoints didn't exist.
        Listener methods (@KafkaListener etc.) were not handled at all.
        """
        classes = self.classes
        results: list[dict] = []
        kind = (kind or "rest").lower()

        for cls in classes.values():
            node_type = cls.get("node_type", "")
            is_grpc = node_type == "grpc"

            if type_filter and node_type != type_filter:
                continue

            for m in cls.get("methods", []):
                hm = m.get("http_method")

                # gRPC service methods carry no http_method in the parser output —
                # synthesize "RPC" so they're categorizable and displayable.
                if not hm:
                    if not is_grpc:
                        continue
                    hm = RPC_VERB

                category = categorize_verb(hm)
                if kind != "all" and category != kind:
                    continue

                if method_filter and hm.upper() != method_filter.upper():
                    continue

                # For gRPC, fall back to "/methodName" as a readable path
                ep_path = m.get("http_path") or (f"/{m.get('name', '')}" if is_grpc else "/")
                if path_filter and path_filter not in ep_path:
                    continue

                results.append({
                    "http_method": hm,
                    "category": category,
                    "path": ep_path,
                    "controller": cls["name"],
                    "file": cls.get("file_path", ""),
                    "handler": m.get("name", ""),
                    "signature": m.get("signature", ""),
                    "request_body": m.get("request_body_type"),
                    "return_type": m.get("return_type", ""),
                    "source": cls.get("source", "java"),
                })

        return sorted(results, key=lambda x: (x["path"], x["http_method"]))

    # ── Stats ─────────────────────────────────

    def stats(self) -> StatsResult:
        data = self._load()
        classes = data.get("classes", {})

        by_type: dict[str, int] = {}
        total_methods = 0
        rest_count = 0
        grpc_count = 0
        listener_count = 0
        regex_fb = 0
        openapi_n = 0
        proto_n = 0

        for cls in classes.values():
            t = cls.get("node_type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
            methods = cls.get("methods", [])
            total_methods += len(methods)

            is_grpc_cls = t == "grpc"
            for m in methods:
                hm = m.get("http_method")
                if not hm and is_grpc_cls:
                    hm = RPC_VERB  # synthesize, same as list_endpoints()
                if not hm:
                    continue
                category = categorize_verb(hm)
                if category == "rest":
                    rest_count += 1
                elif category == "grpc":
                    grpc_count += 1
                elif category == "listener":
                    listener_count += 1

            if cls.get("parse_error") == "regex-fallback":
                regex_fb += 1
            src = cls.get("source", "java")
            if src == "openapi":
                openapi_n += 1
            elif src == "proto":
                proto_n += 1

        ast_n = len(classes) - regex_fb - openapi_n - proto_n

        def _kb(p: Path) -> float:
            return round(p.stat().st_size / 1024, 1) if p.exists() else 0.0

        return StatsResult(
            project_name=data.get("project_name", "Unknown"),
            generated_at=data.get("generated_at", ""),
            by_type=by_type,
            total_classes=len(classes),
            total_methods=total_methods,
            total_endpoints=rest_count,
            rest_endpoints=rest_count,
            grpc_endpoints=grpc_count,
            listener_endpoints=listener_count,
            ast_parsed=max(0, ast_n),
            regex_fallback=regex_fb,
            openapi_nodes=openapi_n,
            proto_nodes=proto_n,
            graph_json_kb=_kb(self.out_dir / "graph.json"),
            graph_md_kb=_kb(self.out_dir / "GRAPH.md"),
        )