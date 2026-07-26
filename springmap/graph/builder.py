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
import re
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

def _normalize_api_name(name: str) -> str:
    """
    Normalize an interface/API-spec name for fuzzy matching between a Java
    implements-list entry and an OpenAPI-derived virtual node name.

    Real-world naming drifts between the two sources in ways exact string
    matching can't handle:
      - openapi-generator interface convention: "HoldsApi"
      - SpringMap's OpenAPI virtual node naming: "HoldsApiSpec" (tag + "ApiSpec")
      - OpenAPI tags containing underscores: "Wallet_Transactions" tag becomes
        node name "Wallet_TransactionsApiSpec", while the Java interface is
        "WalletTransactionsApi" (no underscore) — a direct string comparison
        would never match these.
    Stripping non-alphanumerics and lowercasing, then comparing with a
    trailing "spec" stripped from the OpenAPI side, resolves both cases.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


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

    BUG FIX: this only ever scanned Java-parsed interfaces (`cls.is_interface`).
    A very common real-world pattern — openapi-generator producing an interface
    like `HoldsApi` that only exists at BUILD time (generated into
    target/generated-sources, which SpringMap correctly excludes from parsing)
    — meant the corresponding Java interface was never present in the graph to
    match against. Meanwhile openapi_parser.py independently creates a virtual
    node from the same spec file (e.g. "HoldsApiSpec") holding the real
    endpoint data, but the two were never connected: the controller's
    `implements: ["HoldsApi"]` entry had nothing to resolve against, so the
    controller's own methods kept http_method=null forever, while the actual
    endpoint data sat orphaned on a same-but-differently-named virtual node
    with no back-reference. On a real project this meant `springmap show
    HoldController` showed zero endpoints for a class that unambiguously IS
    the REST controller for those routes.

    Fix: OpenAPI-derived virtual nodes (node_type=OPENAPI) are now included as
    an endpoint source alongside Java interfaces, matched against a
    controller's implements-list via normalized name comparison (handles both
    the "Api" vs "ApiSpec" suffix difference and underscore/casing drift
    between an OpenAPI tag and the corresponding Java interface name).
    """
    # Step 1: collect interfaces that have endpoints — both Java-parsed
    # interfaces AND OpenAPI-derived virtual nodes.
    iface_endpoints: dict[str, list[MethodInfo]] = {}
    iface_base_paths: dict[str, Optional[str]] = {}
    # Separate index for fuzzy OpenAPI-node matching: normalized name -> node name
    openapi_by_normalized: dict[str, str] = {}

    for cls in graph.classes.values():
        if cls.is_interface and cls.endpoints:
            iface_endpoints[cls.name] = cls.endpoints
            iface_base_paths[cls.name] = cls.base_path
        elif cls.node_type == NodeType.OPENAPI and cls.endpoints:
            iface_endpoints[cls.name] = cls.endpoints
            iface_base_paths[cls.name] = cls.base_path
            # Index under the name with a trailing "spec" stripped, so
            # "HoldsApiSpec" is findable via normalized "holdsapi"
            norm = _normalize_api_name(cls.name)
            if norm.endswith("spec"):
                norm = norm[: -len("spec")]
            openapi_by_normalized[norm] = cls.name

    if not iface_endpoints:
        return

    # Step 2: propagate to implementing classes
    for cls in graph.classes.values():
        if cls.is_interface:
            continue
        for iface_ref in cls.implements:
            bare = iface_ref.split("<")[0].strip()

            # Exact match against a Java interface or OpenAPI node name first
            resolved_key = bare if bare in iface_endpoints else None

            # Fall back to normalized match against OpenAPI-derived nodes —
            # handles "HoldsApi" -> "HoldsApiSpec" and underscore/case drift
            if resolved_key is None:
                norm = _normalize_api_name(bare)
                resolved_key = openapi_by_normalized.get(norm)

            if resolved_key is None:
                continue

            existing_names = {m.name for m in cls.methods}
            inherited_base = iface_base_paths.get(resolved_key)
            # Fallback only — every ep.http_path below is already a complete
            # path (built via _combine_paths at parse time, whichever source
            # produced it), so effective_base is never used to re-prefix a
            # non-empty path. See docstring above for why re-prefixing was a bug.
            effective_base = cls.base_path or inherited_base

            for ep in iface_endpoints[resolved_key]:
                if ep.name in existing_names:
                    # Method exists — fill in endpoint metadata using the
                    # source's ALREADY-COMPLETE path, verbatim.
                    for m in cls.methods:
                        if m.name == ep.name and m.http_method is None:
                            m.http_method = ep.http_method
                            m.http_path = ep.http_path or (
                                (effective_base or "").rstrip("/") + "/"
                            )
                            m.request_body_type = ep.request_body_type
                    continue

                # Method not in class — add a copy of the already-complete
                # endpoint as-is. No re-prefixing: ep.http_path was built via
                # _combine_paths using ITS OWN source's base_path already: an
                # interface's @RequestMapping, or an OpenAPI spec's servers[].url.
                # Re-adding effective_base here duplicated that base path any
                # time the implementing class had no @RequestMapping of its own
                # (the standard, near-universal pattern for this exact codegen
                # style — the interface/spec carries all mapping information,
                # the impl class just @Override's the methods).
                from copy import deepcopy
                new_ep = deepcopy(ep)
                if not new_ep.http_path and effective_base:
                    new_ep.http_path = effective_base.rstrip("/") + "/"
                cls.methods.append(new_ep)
                log.debug(
                    "Interface discovery: added %s.%s from %s",
                    cls.name, new_ep.name, resolved_key,
                )


def _detect_grpc_implementation_status(graph: ProjectGraph) -> None:
    """
    For every proto-sourced gRPC service (a `service X { rpc ... }` block
    parsed from a .proto file), determine whether THIS repo actually
    implements it as a server, or only holds the proto definition — and its
    generated client-stub code — to call it as a CLIENT of some other,
    separately-deployed service.

    Why this matters: a project can include a .proto file purely to generate
    a client stub for calling ANOTHER team's gRPC service. Before this pass,
    every service{} block found in any .proto file under the project became
    an identical NodeType.GRPC node with no distinction — so `springmap
    endpoints --grpc` would report RPCs the project merely CALLS as if they
    were endpoints the project PROVIDES. That's a real correctness gap for
    any project with client-only proto dependencies, distinct from (and not
    fixed by) the earlier NodeType.GRPC classification fixes, which were
    about classifying gRPC IMPL classes correctly, not about whether an impl
    exists at all.

    The signal: protoc-gen-grpc-java always generates {ServiceName}Grpc as
    the outer wrapper class, with {ServiceName}Grpc.{ServiceName}ImplBase as
    the abstract SERVER base (extended to implement the service) and
    separate Stub / BlockingStub / FutureStub nested classes for CLIENT use
    (instantiated via a static factory method, never extended). A Java class
    in this repo whose `extends` resolves to "{ServiceName}ImplBase" —
    _resolve_type_name walks qualified-type chains to their innermost
    segment, so that's the form `extends` takes here — is therefore
    unambiguous proof of a local server implementation. No such class means
    this repo only consumes the service.
    """
    implemented_service_names: set[str] = set()

    for node in graph.classes.values():
        if node.source != "java" or node.node_type != NodeType.GRPC or not node.extends:
            continue
        ext = node.extends
        if ext.lower().endswith("implbase"):
            implemented_service_names.add(ext[: -len("ImplBase")])
        elif "grpc" in ext.lower():
            # Fallback for the outer-wrapper-name form (bare "XGrpc"), in
            # case some javalang version/qualifier depth surfaces that
            # instead of the inner ImplBase segment.
            implemented_service_names.add(re.sub(r"Grpc$", "", ext, flags=re.IGNORECASE))

    for node in graph.classes.values():
        if node.source != "proto" or node.node_type != NodeType.GRPC:
            continue
        # BUG FIX: node.name may have been renamed by _insert_proto_node's
        # collision handling (e.g. "HoldService" -> "HoldServiceGrpcContract"
        # when a same-named Java class exists for an unrelated reason). The
        # ORIGINAL proto service name — which is what needs to match against
        # implemented_service_names above — is preserved in the GrpcService
        # annotation's "value" attribute, set once at creation time in
        # proto_parser.py and never touched by the rename. Matching on the
        # live (possibly-renamed) node.name here silently marked correctly-
        # implemented services as client-only, because their renamed name
        # never matched anything in implemented_service_names.
        original_name = node.name
        for ann in node.annotations:
            if ann.name == "GrpcService":
                original_name = ann.attributes.get("value", node.name)
                break
        node.is_locally_implemented = original_name in implemented_service_names


# Node types Spring can actually instantiate and inject via DI / component
# scanning. Entities, DTOs, exceptions, utils, enums-as-unknown, and OpenAPI/
# proto-derived virtual nodes are never legitimate injection sources OR targets.
_INJECTABLE_NODE_TYPES = frozenset({
    NodeType.CONTROLLER, NodeType.SERVICE, NodeType.REPOSITORY,
    NodeType.COMPONENT, NodeType.CONFIGURATION, NodeType.GRPC,
    NodeType.MAIN, NodeType.INTERFACE,  # INTERFACE: injecting by contract type (DIP) is standard
})


def _detect_constructor_injection(graph: ProjectGraph) -> None:
    """
    Spring Boot 2.x+ encourages constructor injection without @Autowired.
    If a class has no @Autowired fields but has fields whose types match
    known Spring beans in the graph, mark them as injected.

    BUG FIX: this previously ran on EVERY class regardless of type, and
    treated ANY field whose type matched a known class name as "injected" —
    with no check on whether that class was actually a Spring-managed bean.
    On a real project this meant:
      - @Entity fields with @ManyToOne/@OneToMany JPA relationships (which
        already carry an explicit `field.relationship` marker set by the
        annotation parser) were marked is_injected=True, as if an @Entity
        had `@Autowired` fields — which Spring never does.
      - Fields typed as an enum (e.g. `HoldStatus walletStatus`) were marked
        is_injected=True purely because the enum happened to also be a
        "known class" in the registry, producing nonsensical "dependencies"
        like an @Entity "injecting" an enum value.
    This corrupted the DI graph (`dependencies`/`dependents` edges) for every
    entity with a relationship or enum field — which is most entities in a
    typical Spring Boot + JPA project.

    Fix: only run this heuristic on nodes whose OWN type is one Spring can
    actually instantiate via DI, only consider fields that don't already
    carry a JPA `relationship` marker, and only mark a field injected if its
    TARGET type is itself an injectable node type.
    """
    node_types: dict[str, NodeType] = {n.name: n.node_type for n in graph.classes.values()}

    for node in graph.classes.values():
        if node.node_type not in _INJECTABLE_NODE_TYPES:
            continue  # @Entity, DTO, exception, util, enum-as-unknown, etc. never receive DI

        already = any(f.is_injected for f in node.fields)
        if already:
            continue

        for field in node.fields:
            if field.relationship:
                continue  # JPA @OneToMany/@ManyToOne/etc. — not a Spring DI dependency
            bare = field.type.split("<")[0].strip()
            if bare == node.name:
                continue
            target_type = node_types.get(bare)
            if target_type in _INJECTABLE_NODE_TYPES:
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
# OpenAPI multi-file tag collision resolution
# ─────────────────────────────────────────────

def _insert_openapi_node(graph: ProjectGraph, node: ClassNode) -> None:
    """
    Insert an OpenAPI-derived ClassNode into the graph, merging endpoints
    instead of silently overwriting when two spec files share the same tag.

    BUG FIX: the previous logic was `graph.classes[node.name] = node`, an
    unconditional overwrite. Splitting API specs across multiple YAML files
    by bounded context (e.g. auth-service.yaml and admin-service.yaml both
    tagging endpoints "Users") is a common real-world pattern — each file
    produces a virtual node with the SAME name ("UsersApiSpec"), and every
    file parsed after the first silently discarded all endpoints from the
    ones before it, with no warning.

    Unlike the proto/Java collision fix below (which renames, because those
    two nodes represent genuinely different things), OpenAPI-tag collisions
    represent the SAME logical API grouping physically split across files —
    so the correct resolution is to MERGE endpoint lists, deduplicating by
    method name, rather than rename or overwrite.
    """
    if node.name not in graph.classes:
        graph.classes[node.name] = node
        return

    existing = graph.classes[node.name]
    if existing.source != "openapi":
        # Colliding with a Java/proto class of the same name is a different
        # situation than a same-tag multi-file merge; don't clobber it.
        return

    existing_method_names = {m.name for m in existing.methods}
    for m in node.methods:
        if m.name not in existing_method_names:
            existing.methods.append(m)
            existing_method_names.add(m.name)
    # Record the additional source file for traceability, without discarding
    # the first file's path.
    if node.file_path and node.file_path not in existing.file_path:
        existing.file_path = f"{existing.file_path}, {node.file_path}"


# ─────────────────────────────────────────────
# Proto/Java name collision resolution
# ─────────────────────────────────────────────

def _insert_proto_node(graph: ProjectGraph, node: ClassNode) -> None:
    """
    Insert a .proto-derived ClassNode into the graph, resolving name collisions
    instead of silently dropping data.

    BUG FIX: a .proto file's `service Foo { ... }` block commonly shares a name
    with an existing Java business interface (e.g. both named "HoldService" —
    one is the REST/business-logic contract, the other is the .proto RPC
    definition). The previous logic was `if node.name not in graph.classes`,
    which silently discarded the ENTIRE proto-derived node — including all its
    RPC methods — whenever this collision occurred, with no warning. On a real
    project this meant the gRPC service was completely invisible in the graph
    despite the .proto file being parsed successfully.

    Fix: on collision, rename the proto node with a "GrpcContract" suffix so
    both the Java interface and the proto service definition are preserved as
    distinct, queryable nodes.
    """
    if node.name not in graph.classes:
        graph.classes[node.name] = node
        return

    existing = graph.classes[node.name]
    if existing.source == "java":
        renamed = f"{node.name}GrpcContract"
        suffix = 2
        while renamed in graph.classes:
            renamed = f"{node.name}GrpcContract{suffix}"
            suffix += 1
        node.name = renamed
        graph.classes[renamed] = node
    # else: duplicate proto/openapi definition of the same name — keep the
    # first one seen rather than overwrite, since both came from generated
    # sources and neither is more authoritative.


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
        artifact_id=pom["artifact_id"],
        group_id=pom["group_id"],
        project_version=pom["version"],
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

    # OpenAPI specs — same-tag collisions across files are merged, never dropped
    for node in parse_all_openapi(project_root):
        _insert_openapi_node(graph, node)

    # .proto files — collisions are renamed, never silently dropped
    for node in parse_all_proto(project_root):
        _insert_proto_node(graph, node)

    # Post-parse passes
    _interface_endpoint_discovery(graph)
    _detect_grpc_implementation_status(graph)
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
    _detect_grpc_implementation_status(existing)
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
        _insert_openapi_node(graph, node)
    for node in parse_all_proto(project_root):
        _insert_proto_node(graph, node)