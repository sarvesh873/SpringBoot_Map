"""
Protocol Buffers (.proto) parser.

Produces ClassNode objects for every gRPC service (node_type=GRPC) and
every message type that looks like an entity or DTO.  Uses regex — no
protobuf library dependency needed.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from springmap.graph.models import (
    AnnotationInfo,
    ClassNode,
    FieldInfo,
    MethodInfo,
    NodeType,
    ParamInfo,
)

# ─────────────────────────────────────────────
# Regex patterns
# ─────────────────────────────────────────────

_RE_SYNTAX = re.compile(r'syntax\s*=\s*"([^"]+)"')
_RE_PACKAGE = re.compile(r"^package\s+([\w.]+)\s*;", re.MULTILINE)
_RE_OPTION_JAVA_PKG = re.compile(r'option\s+java_package\s*=\s*"([^"]+)"')
_RE_OPTION_JAVA_CLASS = re.compile(r'option\s+java_outer_classname\s*=\s*"([^"]+)"')

# service Foo { ... }
_RE_SERVICE = re.compile(r"service\s+(\w+)\s*\{([^{}]+)\}", re.DOTALL)
# rpc MethodName (RequestType) returns (ResponseType);
_RE_RPC = re.compile(
    r"rpc\s+(\w+)\s*\(\s*(stream\s+)?(\w+)\s*\)\s+returns\s*\(\s*(stream\s+)?(\w+)\s*\)"
)

# message Foo { ... }  — non-recursive, handles single-level nesting heuristically
_RE_MESSAGE = re.compile(r"message\s+(\w+)\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", re.DOTALL)
# field:  [repeated] type name = N;
#
# NOTE: no longer anchored to line-start (^) with MULTILINE — the previous
# version only matched the FIRST field-like statement per physical line, so
# a message body with multiple field declarations crammed onto one line
# (unusual, but not invalid proto3 syntax) silently lost every field after
# the first on that line. Doesn't require a preceding newline; each field
# declaration is self-contained (type + name + "= N") so removing the
# anchor doesn't introduce false positives.
_RE_PROTO_FIELD = re.compile(
    r"(?:repeated\s+|optional\s+|required\s+)?(\w+)\s+(\w+)\s*=\s*\d+",
)
# map<KeyType, ValueType> name = N;  — the standard scalar-field regex above
# requires a single \w+ token for the type, which never matches "map<K, V>"
# (angle brackets and the comma aren't \w characters), so map fields need
# their own pattern. Same line-anchor removal as above, for the same reason.
_RE_PROTO_MAP_FIELD = re.compile(
    r"map\s*<\s*(\w+)\s*,\s*(\w+)\s*>\s*(\w+)\s*=\s*\d+",
)

# Maps proto scalar types to Java types
_SCALAR_MAP: dict[str, str] = {
    "double": "Double",
    "float": "Float",
    "int32": "Integer",
    "int64": "Long",
    "uint32": "Integer",
    "uint64": "Long",
    "sint32": "Integer",
    "sint64": "Long",
    "fixed32": "Integer",
    "fixed64": "Long",
    "sfixed32": "Integer",
    "sfixed64": "Long",
    "bool": "Boolean",
    "string": "String",
    "bytes": "byte[]",
}


def _java_type(proto_type: str) -> str:
    return _SCALAR_MAP.get(proto_type, proto_type)


def _strip_comments(source: str) -> str:
    """Remove // and /* */ comments from proto source."""
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return source


# ─────────────────────────────────────────────
# File discovery
# ─────────────────────────────────────────────

def find_proto_files(project_root: str) -> list[Path]:
    root = Path(project_root)
    return [
        p for p in root.rglob("*.proto")
        if not any(part.startswith(".") for part in p.parts)
        and "build" not in p.parts
        and "target" not in p.parts
    ]


# ─────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────

def parse_proto_file(path: Path, project_root: str) -> list[ClassNode]:
    """
    Parse a single .proto file.
    Returns:
      - One ClassNode(GRPC) per service
      - One ClassNode(ENTITY) per message type used as a top-level response/request
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Cannot read %s: %s", path, exc)
        return []

    clean = _strip_comments(source)
    rel_path = str(path.relative_to(Path(project_root)))

    pkg_match = _RE_OPTION_JAVA_PKG.search(clean) or _RE_PACKAGE.search(clean)
    package = pkg_match.group(1) if pkg_match else "proto.generated"

    # ── Collect all message types for type resolution ──
    #
    # BUG FIX (map fields): the scalar _RE_PROTO_FIELD pattern requires a
    # single-word type token, so `map<string, string> metadata = 2;` never
    # matched and was silently dropped from the message's field list. Map
    # fields are now matched separately via _RE_PROTO_MAP_FIELD and rendered
    # as a Java-style Map<K, V> type — confirmed missing in real output
    # (ReleaseHoldRequestGRPC.metadata) before this fix.
    messages: dict[str, list[FieldInfo]] = {}
    for m in _RE_MESSAGE.finditer(clean):
        msg_name = m.group(1)
        body = m.group(2)

        # Map fields first, so their span doesn't also get partially matched
        # by the scalar pattern below (map<string,string> metadata = 2; has
        # no standalone \w+ \w+ = N shape that would double-match, but this
        # keeps map-field lines out of the scalar loop's line-by-line scan).
        map_field_lines: set[int] = set()
        fields: list[FieldInfo] = []
        for fm in _RE_PROTO_MAP_FIELD.finditer(body):
            key_type, val_type, fname = fm.group(1), fm.group(2), fm.group(3)
            fields.append(FieldInfo(
                name=fname,
                type=f"Map<{_java_type(key_type)}, {_java_type(val_type)}>",
            ))
            map_field_lines.add(fm.start())

        for fm in _RE_PROTO_FIELD.finditer(body):
            proto_type, fname = fm.group(1), fm.group(2)
            if fname in ("reserved", "option", "oneof", "map"):
                continue
            if proto_type == "map":
                continue  # already captured by the map-field pass above
            fields.append(FieldInfo(name=fname, type=_java_type(proto_type)))
        messages[msg_name] = fields

    nodes: list[ClassNode] = []
    rpc_types_used: set[str] = set()

    # ── Parse services → GRPC ClassNodes ──
    for svc_match in _RE_SERVICE.finditer(clean):
        svc_name = svc_match.group(1)
        svc_body = svc_match.group(2)
        methods: list[MethodInfo] = []

        for rpc in _RE_RPC.finditer(svc_body):
            rpc_name = rpc.group(1)
            req_stream = bool(rpc.group(2))
            req_type = rpc.group(3)
            resp_stream = bool(rpc.group(4))
            resp_type = rpc.group(5)

            rpc_types_used.add(req_type)
            rpc_types_used.add(resp_type)

            req_java = f"StreamObserver<{req_type}>" if req_stream else req_type
            resp_java = f"StreamObserver<{resp_type}>" if resp_stream else resp_type

            mi = MethodInfo(
                name=rpc_name[0].lower() + rpc_name[1:],
                return_type=resp_java,
                parameters=[
                    ParamInfo(type=req_java, name="request"),
                    ParamInfo(type=f"StreamObserver<{resp_type}>", name="responseObserver"),
                ],
                annotations=[AnnotationInfo(name="GrpcMethod", attributes={"value": rpc_name})],
            )
            methods.append(mi)

        grpc_node = ClassNode(
            name=svc_name,
            package=package,
            file_path=rel_path,
            node_type=NodeType.GRPC,
            methods=methods,
            source="proto",
            annotations=[AnnotationInfo(name="GrpcService", attributes={"value": svc_name})],
        )
        nodes.append(grpc_node)
        log.debug("Proto: gRPC service %s with %d RPCs", svc_name, len(methods))

    # ── Transitive closure: include message types referenced as a FIELD of
    # an already-included message, not just types used directly at the top
    # level of an RPC signature. ──
    #
    # BUG FIX: a message like ListHoldsRequestGRPC (a top-level RPC request
    # type, so included) commonly has a field `PaginationRequestGRPC
    # pagination = 4;` — a message type that is itself NEVER a top-level RPC
    # request/response, only ever referenced as a field of another message.
    # The old code only ever checked rpc_types_used (direct top-level
    # references), so PaginationRequestGRPC/PaginationResponseGRPC-style
    # types were silently excluded from the graph entirely, even though
    # they're real types actively used by real, included RPC messages.
    # Confirmed by their absence in real output despite being referenced.
    included = set(rpc_types_used)
    worklist = list(rpc_types_used)
    while worklist:
        current = worklist.pop()
        for field in messages.get(current, []):
            # Strip Map<K, V> / List<T> wrappers to find the bare referenced type
            candidates = re.findall(r"[A-Za-z_]\w*", field.type)
            for cand in candidates:
                if cand in messages and cand not in included:
                    included.add(cand)
                    worklist.append(cand)

    # ── Create entity/DTO nodes for every message type in the transitive
    # closure of what RPC signatures actually reference. ──
    for msg_name, fields in messages.items():
        if msg_name not in included:
            continue  # Not reachable from any RPC signature — skip
        msg_node = ClassNode(
            name=msg_name,
            package=package,
            file_path=rel_path,
            node_type=NodeType.DTO,
            fields=fields,
            source="proto",
            annotations=[AnnotationInfo(name="ProtoMessage", attributes={"value": msg_name})],
        )
        nodes.append(msg_node)

    return nodes


def parse_all_proto(project_root: str) -> list[ClassNode]:
    """Discover and parse all .proto files."""
    files = find_proto_files(project_root)
    if files:
        log.info("Found %d .proto file(s): %s", len(files), [f.name for f in files])

    nodes: list[ClassNode] = []
    for f in files:
        nodes.extend(parse_proto_file(f, project_root))
    return nodes