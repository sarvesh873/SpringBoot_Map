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
_RE_PROTO_FIELD = re.compile(
    r"^\s*(?:repeated\s+|optional\s+|required\s+)?(\w+)\s+(\w+)\s*=\s*\d+",
    re.MULTILINE,
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
    messages: dict[str, list[FieldInfo]] = {}
    for m in _RE_MESSAGE.finditer(clean):
        msg_name = m.group(1)
        body = m.group(2)
        fields: list[FieldInfo] = []
        for fm in _RE_PROTO_FIELD.finditer(body):
            proto_type, fname = fm.group(1), fm.group(2)
            if fname in ("reserved", "option", "oneof", "map"):
                continue
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

    # ── Create entity/DTO nodes for message types used in RPC signatures ──
    for msg_name, fields in messages.items():
        if msg_name not in rpc_types_used:
            continue  # Only create nodes for types referenced in service contracts
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