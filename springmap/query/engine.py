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

BUG FIX (2024): list_endpoints() used to return gRPC RPCs mixed in with REST
endpoints by default, and had no way to surface Kafka/RabbitMQ listener methods
at all. It now defaults to kind='rest' and exposes 'grpc' / 'listener' / 'all'
as explicit opt-ins, using the same HTTP_VERBS / LISTENER_VERBS categorization
that java_parser.py and models.py use — so a method's classification is
identical everywhere in the codebase.
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


# ─────────────────────────────────────────────
# project_info() supporting types + constants
# ─────────────────────────────────────────────

@dataclass
class _DbAnalysis:
    url: Optional[str]
    driver: Optional[str]
    inferred_type: str            # "PostgreSQL", "MySQL", "H2", "Unknown", …
    canonical_artifact: str       # expected Maven artifact for this DB type
    dependency_found: bool        # canonical_artifact found in pom.xml?
    matching_dep: Optional[str]   # the actual dep string that matched
    mismatch: bool                # URL implies DB type but no matching dep found


@dataclass
class _StarterInfo:
    artifact: str     # full artifact string from pom (e.g. "org.springframework.boot:spring-boot-starter-web")
    label: str        # human label (e.g. "Web (MVC / REST)")
    note: str         # graph-derived annotation (e.g. "4 controllers, 18 REST endpoints")


@dataclass
class ProjectInfoResult:
    # Maven coordinates
    project_name: str
    group_id: str
    artifact_id: str
    project_version: str
    # Tech stack
    java_version: str
    spring_boot_version: str
    # Server runtime
    server_port: str
    context_path: str
    active_profiles: list[str]
    # Database
    db: _DbAnalysis
    jpa_ddl_auto: Optional[str]
    jpa_show_sql: bool
    # Dependencies
    total_deps: int
    starters: list[_StarterInfo]
    db_deps: list[str]
    messaging_deps: list[str]
    security_deps: list[str]
    testing_deps: list[str]
    other_deps: list[str]
    # Config
    custom_props: dict[str, str]


# (url_substring, display_name, canonical_maven_artifact)
_DB_PATTERNS: list[tuple[str, str, str]] = [
    ("postgresql", "PostgreSQL",  "org.postgresql:postgresql"),
    ("postgres",   "PostgreSQL",  "org.postgresql:postgresql"),
    ("mysql",      "MySQL",       "mysql:mysql-connector-java"),
    ("mariadb",    "MariaDB",     "org.mariadb.jdbc:mariadb-java-client"),
    ("oracle",     "Oracle DB",   "com.oracle.database.jdbc:ojdbc8"),
    ("sqlserver",  "SQL Server",  "com.microsoft.sqlserver:mssql-jdbc"),
    ("db2",        "IBM DB2",     "com.ibm.db2:jcc"),
    ("h2",         "H2",          "com.h2database:h2"),
    ("hsqldb",     "HSQLDB",      "org.hsqldb:hsqldb"),
    ("mongodb",    "MongoDB",     "spring-boot-starter-data-mongodb"),
    ("redis",      "Redis",       "spring-boot-starter-data-redis"),
    ("cassandra",  "Cassandra",   "spring-boot-starter-data-cassandra"),
    ("r2dbc",      "R2DBC",       "spring-boot-starter-data-r2dbc"),
]

def _n(cnt: int, singular: str, plural: str = "") -> str:
    if cnt == 0:
        return ""
    label = plural if (cnt > 1 and plural) else singular
    return f"{cnt} {label}"

# (artifact_key, display_label, note_fn(rest,grpc,kafka,rabbit,sqs,event,sched,listener,entity,repo)->str)
_KEY_STARTERS: list[tuple[str, str, object]] = [
    ("spring-boot-starter-web",
     "Web (MVC / REST)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(r,"REST endpoint","REST endpoints")),
    ("spring-boot-starter-webflux",
     "WebFlux (Reactive)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(r,"reactive endpoint","reactive endpoints")),
    ("spring-boot-starter-data-jpa",
     "JPA / Hibernate",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp:
         ", ".join(x for x in [_n(en,"entity","entities"), _n(rp,"repository","repositories")] if x)),
    ("spring-boot-starter-data-mongodb",
     "MongoDB",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(rp,"repository","repositories")),
    ("spring-boot-starter-data-redis",
     "Redis",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-boot-starter-data-r2dbc",
     "R2DBC (Reactive DB)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(rp,"repository","repositories")),
    ("spring-boot-starter-kafka",
     "Kafka",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(ka,"@KafkaListener","@KafkaListeners")),
    ("spring-kafka",
     "Kafka (direct)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(ka,"@KafkaListener","@KafkaListeners")),
    ("spring-boot-starter-amqp",
     "RabbitMQ",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(rb,"@RabbitListener","@RabbitListeners")),
    ("io.awspring",
     "AWS Spring Cloud (SQS/SNS/S3)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(sq,"@SqsListener","@SqsListeners")),
    ("spring-cloud-aws",
     "Spring Cloud AWS",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(sq,"@SqsListener","@SqsListeners")),
    ("spring-boot-starter-security",
     "Spring Security",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-boot-starter-oauth2",
     "OAuth2",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-boot-starter-actuator",
     "Actuator",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-boot-starter-validation",
     "Bean Validation",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-boot-starter-mail",
     "Mail / SMTP",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-boot-starter-cache",
     "Caching",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-boot-starter-aop",
     "AOP",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-boot-starter-test",
     "Test",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-cloud-starter-openfeign",
     "OpenFeign (HTTP clients)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-cloud-starter-config",
     "Config Client",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("spring-cloud-starter-netflix-eureka",
     "Eureka Discovery",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("grpc-spring-boot-starter",
     "gRPC Server",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(g,"RPC","RPCs")),
    ("net.devh:grpc",
     "gRPC (devh)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: _n(g,"RPC","RPCs")),
    ("protobuf-java",
     "Protocol Buffers",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("springdoc-openapi",
     "SpringDoc (OpenAPI UI)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("springfox",
     "SpringFox (Swagger UI)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("flyway",
     "Flyway DB Migrations",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("liquibase",
     "Liquibase DB Migrations",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("mapstruct",
     "MapStruct",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("lombok",
     "Lombok",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("resilience4j",
     "Resilience4j (Circuit Breaker)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("micrometer",
     "Micrometer (Metrics)",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
    ("testcontainers",
     "Testcontainers",
     lambda r,g,ka,rb,sq,ev,sc,l,en,rp: ""),
]

_DB_KW   = {"postgresql","mysql","mariadb","oracle","h2","hsqldb","jdbc","hibernate",
             "flyway","liquibase","mongodb","redis","cassandra","jpa","r2dbc","db2"}
_MSG_KW  = {"kafka","rabbit","amqp","jms","activemq","pulsar","sqs","sns","awspring","messaging"}
_SEC_KW  = {"security","oauth","jwt","keycloak","nimbus","jasypt","encrypt"}
_TEST_KW = {"test","junit","mockito","testcontainer","wiremock","assertj","hamcrest","archunit"}


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
        #
        # BUG FIX: this loop only ever read `m.get("http_method")` directly
        # and skipped the method entirely if it was empty/None. gRPC RPC
        # methods carry no http_method in the raw parsed data (unlike REST
        # endpoints) — list_endpoints() synthesizes "RPC" for them, but this
        # search path never did, so `query "kind:grpc"` always returned zero
        # endpoint matches regardless of how many gRPC services existed.
        # Now synthesizes the same way list_endpoints() does, and applies the
        # same "only locally-implemented services count as gRPC endpoints
        # this project provides" rule — see list_endpoints()'s docstring for
        # why proto-sourced services without a matching Java server impl are
        # excluded by default (client-stub-only proto dependencies for
        # calling another team's service should not be reported as this
        # project's own endpoints).
        if "path" in filters or "method" in filters or "kind" in filters:
            requested_kind = filters.get("kind", "").lower()
            for cls in classes.values():
                node_type = cls.get("node_type", "")
                is_grpc_cls = node_type == "grpc"
                for m in cls.get("methods", []):
                    hm = m.get("http_method")
                    if not hm:
                        if not is_grpc_cls:
                            continue
                        hm = RPC_VERB

                    category = categorize_verb(hm)
                    if requested_kind and category != requested_kind:
                        continue
                    if category == "grpc" and cls.get("source") == "proto":
                        # A locally-implemented proto service's RPCs are
                        # already represented by its Java impl node
                        # (source="java") — showing the proto contract too
                        # would duplicate every RPC. A client-only service
                        # (no local impl) isn't this project's own endpoint
                        # either. search() has no --clients-equivalent
                        # opt-in, so proto-sourced grpc nodes are always
                        # skipped here; use `endpoints --grpc --clients` to
                        # see client-only services explicitly.
                        continue

                    if "path" in filters and filters["path"] not in (m.get("http_path") or ""):
                        continue
                    if "method" in filters and hm.upper() != filters["method"].upper():
                        continue
                    # BUG FIX: `hm` above may be a synthesized value (RPC_VERB
                    # for gRPC methods, which carry no http_method in the raw
                    # parsed data). The raw `m` dict still has
                    # http_method=None regardless — appending it as-is here
                    # meant every synthesized-verb result silently carried a
                    # None http_method downstream, which crashed cli.py's
                    # query command (`method.get("http_method").upper()` on
                    # None) the moment this loop started actually returning
                    # gRPC matches (previously it never did — see the "never
                    # synthesizes RPC verb" fix above this block). Store a
                    # copy with the resolved verb written in instead of the
                    # untouched original.
                    method_entry = dict(m)
                    method_entry["http_method"] = hm
                    matched_endpoints.append({"cls": cls, "method": method_entry})

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
        include_client_only: bool = False,
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

        BUG FIX (client vs. server gRPC): a project can vendor a .proto file
        purely to generate a CLIENT stub for calling another team's gRPC
        service — this project never implements it, only calls out to it.
        Previously every service{} block parsed from any .proto file became
        an identical NodeType.GRPC node with no distinction, so kind='grpc'
        reported RPCs the project merely calls as if they were endpoints the
        project provides. Proto-sourced gRPC services are now tagged
        is_locally_implemented (True if a Java class in this repo extends
        the protoc-generated {ServiceName}ImplBase — see
        _detect_grpc_implementation_status in builder.py) and, by default,
        only locally-implemented ones are returned for kind='grpc'. Pass
        include_client_only=True to see client-stub-only services too — they
        are never silently hidden, just excluded from the default view that
        frames itself as "this project's own endpoints."
        """
        classes = self.classes
        results: list[dict] = []
        kind = (kind or "rest").lower()

        for cls in classes.values():
            node_type = cls.get("node_type", "")
            is_grpc = node_type == "grpc"

            if type_filter and node_type != type_filter:
                continue

            if is_grpc and cls.get("source") == "proto":
                is_local = cls.get("is_locally_implemented")
                if is_local is True:
                    # A Java-sourced GRPC node (source="java") already
                    # represents these exact RPCs with real, authoritative
                    # Java method signatures — showing the proto contract's
                    # placeholder-typed version alongside it would just
                    # duplicate every RPC. Skip the proto node entirely;
                    # the Java impl node covers it.
                    continue
                if is_local is False and not include_client_only:
                    # Genuine client-only service (no local implementation
                    # anywhere) — hidden unless explicitly requested.
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
                    "is_locally_implemented": cls.get("is_locally_implemented"),
                })

        return sorted(results, key=lambda x: (x["path"], x["http_method"]))

    # ── Project Info ───────────────────────────

    def project_info(self) -> "ProjectInfoResult":
        """
        Deep analysis of pom.xml + application.yml data stored in graph.json.

        Returns a structured result with:
          - Maven coordinates (groupId:artifactId:version)
          - Java + Spring Boot versions
          - Server port, context path, active profiles
          - Database: type inferred from URL, driver cross-referenced against deps
          - All dependencies grouped by category with graph-count annotations
          - Custom application properties
          - Warnings when configuration and dependencies contradict each other
        """
        data = self._load()
        cfg = data.get("config", {})
        all_deps: list[str] = data.get("maven_dependencies", [])
        classes = data.get("classes", {})

        # Graph counts for annotating dependency entries
        rest_cnt = listener_cnt = grpc_cnt = entity_cnt = repo_cnt = 0
        kafka_cnt = rabbit_cnt = sqs_cnt = event_cnt = sched_cnt = 0
        for cls in classes.values():
            nt = cls.get("node_type", "")
            if nt == "entity":     entity_cnt += 1
            if nt == "repository": repo_cnt   += 1
            is_grpc_cls = nt == "grpc"
            for m in cls.get("methods", []):
                hm = m.get("http_method") or ("RPC" if is_grpc_cls else None)
                if not hm:
                    continue
                cat = categorize_verb(hm)
                if cat == "rest":      rest_cnt     += 1
                elif cat == "grpc":    grpc_cnt     += 1
                elif cat == "listener":
                    listener_cnt += 1
                    v = hm.upper()
                    if v == "KAFKA":     kafka_cnt += 1
                    elif v == "RABBIT":  rabbit_cnt += 1
                    elif v == "SQS":     sqs_cnt   += 1
                    elif v == "EVENT":   event_cnt  += 1
                    elif v == "SCHEDULED": sched_cnt += 1

        # DB inference
        ds_url    = (cfg.get("datasource_url")    or "").strip()
        ds_driver = (cfg.get("datasource_driver") or "").strip()
        url_l = ds_url.lower();   drv_l = ds_driver.lower()

        db_type = db_canonical = db_match = ""
        db_found = False
        for url_key, db_name, canon in _DB_PATTERNS:
            if url_key in url_l or url_key in drv_l:
                db_type = db_name;   db_canonical = canon
                art_name = canon.split(":")[-1]
                for dep in all_deps:
                    if art_name in dep.lower() or url_key in dep.lower():
                        db_found = True;  db_match = dep;  break
                break

        db = _DbAnalysis(
            url=ds_url or None, driver=ds_driver or None,
            inferred_type=db_type or "Unknown",
            canonical_artifact=db_canonical,
            dependency_found=db_found,
            matching_dep=db_match or None,
            mismatch=bool(db_type) and not db_found and bool(all_deps),
        )

        # Starters / key deps — found only; deduplicate by actual artifact string
        deps_l = [d.lower() for d in all_deps]
        found_starters: list[_StarterInfo] = []
        seen_artifacts: set[str] = set()
        for art_key, label, note_fn in _KEY_STARTERS:
            if not any(art_key in d for d in deps_l):
                continue
            actual = next((d for d in all_deps if art_key in d.lower()), art_key)
            if actual in seen_artifacts:
                continue
            seen_artifacts.add(actual)
            note = note_fn(rest_cnt, grpc_cnt, kafka_cnt, rabbit_cnt, sqs_cnt,
                           event_cnt, sched_cnt, listener_cnt, entity_cnt, repo_cnt)
            found_starters.append(_StarterInfo(artifact=actual, label=label, note=note))

        # Remaining deps by category
        covered = {d.lower() for d in all_deps
                   if any(k in d.lower() for k, _, _ in _KEY_STARTERS)}
        db_deps: list[str] = [];  msg_deps: list[str] = []
        sec_deps: list[str] = []; test_deps: list[str] = []; other_deps: list[str] = []
        for dep in sorted(all_deps):
            dl = dep.lower()
            if dl in covered:
                continue
            if any(k in dl for k in _DB_KW):       db_deps.append(dep)
            elif any(k in dl for k in _MSG_KW):    msg_deps.append(dep)
            elif any(k in dl for k in _SEC_KW):    sec_deps.append(dep)
            elif any(k in dl for k in _TEST_KW):   test_deps.append(dep)
            else:                                   other_deps.append(dep)

        return ProjectInfoResult(
            project_name=data.get("project_name", "Unknown"),
            group_id=data.get("group_id") or data.get("base_package", ""),
            artifact_id=data.get("artifact_id") or data.get("project_name", ""),
            project_version=data.get("project_version", ""),
            java_version=data.get("java_version", ""),
            spring_boot_version=data.get("spring_boot_version", ""),
            server_port=cfg.get("server_port", "8080"),
            context_path=cfg.get("context_path", ""),
            active_profiles=cfg.get("active_profiles", []),
            db=db,
            jpa_ddl_auto=cfg.get("jpa_ddl_auto"),
            jpa_show_sql=cfg.get("jpa_show_sql", False),
            total_deps=len(all_deps),
            starters=found_starters,
            db_deps=db_deps,
            messaging_deps=msg_deps,
            security_deps=sec_deps,
            testing_deps=test_deps,
            other_deps=other_deps,
            custom_props=cfg.get("custom_props", {}),
        )

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