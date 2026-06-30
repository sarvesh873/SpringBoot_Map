"""
Java source file parser.

Primary strategy  : javalang AST (Java 8 core syntax, handles most Spring Boot files).
Fallback strategy : regex extraction when javalang fails (Java 14+ records, text blocks, etc.).

Both strategies call _attach_endpoint_annotations() at the end — a line-scan pass
that catches anything the primary parser missed and also corrects empty paths that
result from complex annotation patterns.

ROOT BUG FIXED HERE:
  javalang uses ElementValuePair (NOT MemberValuePair) for key=value annotation attributes.
  The old code called isinstance(item, javalang.tree.MemberValuePair) which always raised
  AttributeError caught silently, returning {} for ALL named annotation attributes. This
  caused: missing base paths, wrong endpoint paths, missing table names, missing listener topics.
"""
from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Optional

try:
    import javalang
    HAS_JAVALANG = True
except ImportError:
    HAS_JAVALANG = False

from springmap.graph.models import (
    AnnotationInfo,
    ClassNode,
    FieldInfo,
    MethodInfo,
    NodeType,
    ParamInfo,
    ANNOTATION_TO_TYPE,
    HTTP_MAPPING_ANNOTATIONS,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Listener / scheduled annotation → method verb
# ─────────────────────────────────────────────

LISTENER_ANNOTATIONS: dict[str, str] = {
    "KafkaListener":              "KAFKA",
    "KafkaHandler":               "KAFKA",
    "RabbitListener":             "RABBIT",
    "RabbitHandler":              "RABBIT",
    "SqsListener":                "SQS",
    "SqsHandler":                 "SQS",
    "JmsListener":                "JMS",
    "EventListener":              "EVENT",
    "TransactionalEventListener": "EVENT",
    "Scheduled":                  "SCHEDULED",
    "StreamListener":             "STREAM",
}

# All method-level endpoint annotations in one dict
_ENDPOINT_ANNOTATIONS: dict[str, str] = {
    **HTTP_MAPPING_ANNOTATIONS,
    **LISTENER_ANNOTATIONS,
}

_JAVA_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "try", "new", "class",
    "interface", "enum", "return", "void", "static", "final", "abstract",
    "synchronized", "default", "super", "this", "throws", "extends",
    "implements", "import", "package", "else", "do", "assert",
})

# ─────────────────────────────────────────────
# Annotation attribute extraction  (THE CRITICAL FIX)
# ─────────────────────────────────────────────

def _ann_attributes(annotation) -> dict[str, str]:
    """
    Extract key→value attributes from a javalang Annotation node.

    CRITICAL: javalang uses ElementValuePair for key=value pairs, NOT MemberValuePair.
    We use hasattr() instead of isinstance() so this works across all javalang versions
    and never raises AttributeError.
    """
    el = annotation.element
    if el is None:
        return {}

    # @Annotation("singleValue")
    if isinstance(el, javalang.tree.Literal):
        return {"value": el.value.strip('"').strip("'")}

    # @Annotation(SomeEnum.VALUE)
    if isinstance(el, javalang.tree.MemberReference):
        return {"value": str(el.member)}

    # @Annotation(key="v1", key2="v2")  ← uses ElementValuePair, not MemberValuePair
    if isinstance(el, list):
        result: dict[str, str] = {}
        for item in el:
            # Use hasattr — avoids isinstance(item, MemberValuePair) which crashes
            if not (hasattr(item, "name") and hasattr(item, "value")):
                continue
            val = item.value
            if isinstance(val, javalang.tree.Literal):
                result[item.name] = val.value.strip('"').strip("'")
            elif isinstance(val, javalang.tree.MemberReference):
                result[item.name] = str(val.member)
            elif hasattr(javalang.tree, "ArrayInitializer") and isinstance(val, javalang.tree.ArrayInitializer):
                values = [
                    v.value.strip('"').strip("'")
                    for v in (val.initializers or [])
                    if isinstance(v, javalang.tree.Literal)
                ]
                result[item.name] = values[0] if len(values) == 1 else (values[0] if values else "")
            else:
                result[item.name] = str(val)
        return result

    return {}


def _parse_annotations(raw_annotations: list) -> list[AnnotationInfo]:
    infos: list[AnnotationInfo] = []
    for ann in (raw_annotations or []):
        try:
            attrs = _ann_attributes(ann)
        except Exception as exc:
            log.debug("Annotation attr parse failed for @%s: %s", ann.name, exc)
            attrs = {}
        infos.append(AnnotationInfo(name=ann.name, attributes=attrs))
    return infos


def _resolve_type_name(ref_type) -> str:
    if ref_type is None:
        return "void"
    if isinstance(ref_type, javalang.tree.BasicType):
        return ref_type.name
    if isinstance(ref_type, javalang.tree.ReferenceType):
        name = ref_type.name
        if ref_type.arguments:
            args = ", ".join(
                _resolve_type_name(a.type)
                for a in ref_type.arguments
                if hasattr(a, "type") and a.type is not None
            )
            return f"{name}<{args}>"
        return name
    return str(ref_type)

# ─────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────

def _get_path(ann_attrs: dict[str, str]) -> Optional[str]:
    return ann_attrs.get("value") or ann_attrs.get("path")


def _combine_paths(base: Optional[str], local: Optional[str]) -> str:
    base = (base or "").rstrip("/")
    local = (local or "")
    if not local.startswith("/"):
        local = "/" + local
    return base + local


def _quick_path_from_line(line: str, ann_name: str, base_path: Optional[str]) -> str:
    """
    Extract the first string literal value from an annotation line using regex.
    Works for: @GetMapping("/path"), @GetMapping(value="/path"), @KafkaListener(topics="name").
    Used by the safety-net pass as an independent fallback from javalang attribute parsing.
    """
    if f"@{ann_name}" not in line:
        return _combine_paths(base_path, "") if ann_name in HTTP_MAPPING_ANNOTATIONS else ""

    # Find the first quoted string in the line after the annotation name
    idx = line.find(f"@{ann_name}")
    m = re.search(r'"([^"]*)"', line[idx:])
    local = m.group(1) if m else ""

    if ann_name in HTTP_MAPPING_ANNOTATIONS:
        return _combine_paths(base_path, local)
    return local  # For listeners: return topic / queue name


# ─────────────────────────────────────────────
# Source-level base-path extraction (safety net)
# ─────────────────────────────────────────────

def _extract_base_path_from_source(source: str, class_pos: int) -> Optional[str]:
    """
    Regex fallback for @RequestMapping base path. Called when javalang attribute
    parsing returns empty (e.g. multi-attribute annotation on older javalang builds).
    Only searches in the area before the class body opens.
    """
    region = source[max(0, class_pos - 500): class_pos + 200]
    m = re.search(
        r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?"([^"]+)"',
        region,
    )
    return m.group(1) if m else None


# ─────────────────────────────────────────────
# Post-parse annotation enrichment pass
# ─────────────────────────────────────────────

def _attach_endpoint_annotations(
    source: str,
    base_path: Optional[str],
    methods: list[MethodInfo],
) -> None:
    """
    Robust post-parse pass: line-scan to pair endpoint/listener annotations
    with the method declarations below them.

    Runs after BOTH javalang and regex parsing:
      - Catches methods whose annotations javalang parsed but got empty path (due to
        named-attribute bugs or multi-attribute annotations).
      - Catches methods missed entirely by the regex fallback.
      - Adds @KafkaListener / @RabbitListener / @EventListener / @Scheduled support.

    Rules:
      - Sets http_method if not already set.
      - Sets http_path  if not already set OR if currently empty while we found a value.
    """
    if not methods:
        return

    by_name = {m.name: m for m in methods}
    lines = source.split("\n")
    n = len(lines)

    i = 0
    while i < n:
        raw = lines[i]
        s = raw.strip()

        if not s or s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            i += 1
            continue

        # Check for a known annotation on this line
        found_verb: Optional[str] = None
        found_path: str = ""

        for ann_name, verb in _ENDPOINT_ANNOTATIONS.items():
            if f"@{ann_name}" in s:
                found_path = _quick_path_from_line(s, ann_name, base_path)
                found_verb = verb
                break

        if found_verb is None:
            i += 1
            continue

        # Scan forward (up to 12 lines) for the method declaration
        for j in range(i + 1, min(i + 13, n)):
            cand = lines[j].strip()
            if not cand:
                continue
            if cand.startswith("//") or cand.startswith("*") or cand.startswith("/*"):
                continue
            if cand.startswith("@"):
                continue  # another annotation — keep scanning
            # Annotation continuation: closing ), or continuation lines with = but no access modifier
            if cand.startswith(")"):
                continue
            if "=" in cand and not re.search(r"\b(?:public|protected|private)\b", cand):
                continue

            # Match: [public|protected|private] [modifiers] ReturnType methodName(
            meth_m = re.search(
                r"\b(?:public|protected|private)\b[^(;{]*?\b(\w+)\s*\(", cand
            )
            if meth_m:
                method_name = meth_m.group(1)
                if method_name not in _JAVA_KEYWORDS:
                    target = by_name.get(method_name)
                    if target:
                        if not target.http_method:
                            # Method completely unannotated — set both
                            target.http_method = found_verb
                            target.http_path = found_path
                        elif not target.http_path and found_path:
                            # Method has verb (from javalang) but empty path — fill path
                            target.http_path = found_path
            break  # Stop at first non-skippable, non-annotation candidate line
        i += 1


# ─────────────────────────────────────────────
# Method call extraction (call graph)
# ─────────────────────────────────────────────

def _extract_calls(method_decl, field_type_map: dict[str, str]) -> list[str]:
    if not method_decl.body:
        return []
    calls: list[str] = []
    try:
        for _, node in method_decl.filter(javalang.tree.MethodInvocation):
            qualifier = node.qualifier
            member = node.member
            if qualifier:
                resolved = field_type_map.get(qualifier, qualifier)
                calls.append(f"{resolved}.{member}()")
    except Exception:
        pass
    return list(dict.fromkeys(calls))


# ─────────────────────────────────────────────
# Node type detection
# ─────────────────────────────────────────────

def _detect_node_type(
    annotations: list[AnnotationInfo],
    class_name: str,
    extends: Optional[str],
    implements: list[str],
    is_interface: bool,
) -> NodeType:
    if is_interface:
        return NodeType.INTERFACE
    for ann in annotations:
        if ann.name in ANNOTATION_TO_TYPE:
            return ANNOTATION_TO_TYPE[ann.name]
    name_lower = class_name.lower()
    if extends and "jparepository" in extends.lower():
        return NodeType.REPOSITORY
    if any("repository" in i.lower() for i in implements):
        return NodeType.REPOSITORY
    if name_lower.endswith("controller"):
        return NodeType.CONTROLLER
    if name_lower.endswith(("service", "serviceimpl")):
        return NodeType.SERVICE
    if name_lower.endswith(("repository", "repo")):
        return NodeType.REPOSITORY
    if name_lower.endswith("entity"):
        return NodeType.ENTITY
    if name_lower.endswith(("dto", "request", "response", "record")):
        return NodeType.DTO
    if name_lower.endswith(("config", "configuration")):
        return NodeType.CONFIGURATION
    if name_lower.endswith(("exception", "error")):
        return NodeType.EXCEPTION
    if name_lower.endswith(("consumer", "producer", "listener", "handler", "processor")):
        return NodeType.COMPONENT
    if name_lower.endswith(("util", "utils", "helper")):
        return NodeType.UTIL
    return NodeType.UNKNOWN


# ─────────────────────────────────────────────
# AST parser (javalang)
# ─────────────────────────────────────────────

def _parse_with_javalang(source: str, file_path: str, rel_path: str) -> Optional[ClassNode]:
    try:
        tree = javalang.parse.parse(source)
    except Exception as exc:
        log.debug("javalang failed on %s: %s", rel_path, exc)
        return None

    package = tree.package.name if tree.package else ""
    imports = [imp.path for imp in (tree.imports or [])]

    for type_decl in (tree.types or []):
        is_interface = isinstance(type_decl, javalang.tree.InterfaceDeclaration)
        if not isinstance(type_decl, (
            javalang.tree.ClassDeclaration,
            javalang.tree.InterfaceDeclaration,
            javalang.tree.EnumDeclaration,
        )):
            continue

        class_name = type_decl.name
        annotations = _parse_annotations(type_decl.annotations)

        extends: Optional[str] = None
        if hasattr(type_decl, "extends") and type_decl.extends:
            ext = type_decl.extends
            extends = _resolve_type_name(ext[0] if isinstance(ext, list) else ext)

        implements: list[str] = []
        if hasattr(type_decl, "implements") and type_decl.implements:
            implements = [_resolve_type_name(i) for i in type_decl.implements]

        node_type = _detect_node_type(annotations, class_name, extends, implements, is_interface)

        # Class-level @RequestMapping base path + @Table name
        base_path: Optional[str] = None
        table_name: Optional[str] = None
        for ann in annotations:
            if ann.name == "RequestMapping":
                base_path = _get_path(ann.attributes)
            if ann.name == "Table":
                table_name = ann.attributes.get("name")
        if node_type == NodeType.ENTITY and table_name is None:
            table_name = _to_snake_case(class_name)

        # Safety net: if base_path still empty, extract from source via regex
        # (guards against any remaining edge cases in _ann_attributes)
        if not base_path:
            class_pos = source.find(class_name)
            extracted = _extract_base_path_from_source(source, class_pos)
            if extracted:
                base_path = extracted

        # Fields
        field_type_map: dict[str, str] = {}
        fields: list[FieldInfo] = []
        for field_decl in (getattr(type_decl, "fields", None) or []):
            raw_type = _resolve_type_name(field_decl.type)
            field_anns = _parse_annotations(field_decl.annotations)
            ann_names = {a.name for a in field_anns}
            is_injected = bool(ann_names & {"Autowired", "Inject", "Resource"})
            is_id = "Id" in ann_names
            column_name = next(
                (a.attributes.get("name") for a in field_anns if a.name == "Column"), None
            )
            relationship = next(
                (a.name for a in field_anns if a.name in
                 {"OneToMany", "ManyToOne", "ManyToMany", "OneToOne"}), None
            )
            config_key = next(
                (a.attributes.get("value", "") for a in field_anns if a.name == "Value"), None
            )
            for decl in field_decl.declarators:
                short = raw_type.split("<")[0]
                camel = short[0].lower() + short[1:] if short else short
                field_type_map[decl.name] = raw_type
                field_type_map[camel] = short
                fields.append(FieldInfo(
                    name=decl.name, type=raw_type, annotations=field_anns,
                    is_injected=is_injected, is_id=is_id,
                    column_name=column_name, relationship=relationship,
                    config_key=config_key,
                ))

        # Constructor injection
        for ctor in (getattr(type_decl, "constructors", None) or []):
            for param in (ctor.parameters or []):
                param_type = _resolve_type_name(param.type)
                for fi in fields:
                    if fi.name == param.name and fi.type == param_type:
                        fi.is_injected = True
                field_type_map[param.name] = param_type

        # Methods
        methods: list[MethodInfo] = []
        for method in (getattr(type_decl, "methods", None) or []):
            m_anns = _parse_annotations(method.annotations)
            m_ann_names = {a.name for a in m_anns}

            http_method: Optional[str] = None
            http_path: Optional[str] = None
            request_body_type: Optional[str] = None

            for ann in m_anns:
                if ann.name in HTTP_MAPPING_ANNOTATIONS:
                    http_method = HTTP_MAPPING_ANNOTATIONS[ann.name]
                    local_path = _get_path(ann.attributes) or ""
                    http_path = _combine_paths(base_path, local_path)
                    break
                if ann.name in LISTENER_ANNOTATIONS:
                    http_method = LISTENER_ANNOTATIONS[ann.name]
                    # Topics / queues can come from 'topics', 'queues', or 'value' key
                    http_path = (
                        ann.attributes.get("topics")
                        or ann.attributes.get("queues")
                        or ann.attributes.get("value")
                        or ""
                    )
                    break

            params: list[ParamInfo] = []
            for p in (method.parameters or []):
                p_anns = [a.name for a in (p.annotations or [])]
                p_type = _resolve_type_name(p.type)
                params.append(ParamInfo(type=p_type, name=p.name, annotations=p_anns))
                if "RequestBody" in p_anns:
                    request_body_type = p_type

            ret_type = "void"
            if method.return_type is not None:
                ret_type = _resolve_type_name(method.return_type)

            calls = _extract_calls(method, field_type_map)
            methods.append(MethodInfo(
                name=method.name,
                return_type=ret_type,
                parameters=params,
                annotations=m_anns,
                calls=calls,
                is_transactional="Transactional" in m_ann_names,
                is_async="Async" in m_ann_names,
                is_scheduled="Scheduled" in m_ann_names,
                http_method=http_method,
                http_path=http_path,
                request_body_type=request_body_type,
                line=method.position.line if method.position else 0,
            ))

        # Safety-net: fills in any endpoint/listener annotations missed above
        # AND corrects empty http_path values even when http_method was set
        _attach_endpoint_annotations(source, base_path, methods)

        return ClassNode(
            name=class_name, package=package, file_path=rel_path,
            node_type=node_type, annotations=annotations,
            fields=fields, methods=methods,
            extends=extends, implements=implements, imports=imports,
            table_name=table_name, base_path=base_path,
            is_interface=is_interface,
        )
    return None


# ─────────────────────────────────────────────
# Regex-based fallback parser
# ─────────────────────────────────────────────

_RE_PACKAGE    = re.compile(r"package\s+([\w.]+)\s*;")
_RE_IMPORT     = re.compile(r"import\s+([\w.]+)\s*;")
_RE_CLASS      = re.compile(
    r"(?:public\s+)?(?:abstract\s+)?(?:final\s+)?"
    r"(class|interface|enum|record)\s+(\w+)"
    r"(?:\s+extends\s+([\w<>, ]+?))?(?:\s+implements\s+([\w<>, ]+?))?\s*[{(]"
)
_RE_ANNOTATION = re.compile(r"@(\w+)(?:\s*\([^)]*\))?")
_RE_FIELD      = re.compile(
    r"(?:private|protected|public|final|static|\s)+"
    r"([\w<>\[\]]+)\s+(\w+)\s*(?:=\s*[^;]+)?\s*;"
)
# Relaxed method regex: does NOT try to parse parameter list (avoids failing on annotated params)
_RE_METHOD = re.compile(
    r"(?:^|\n)\s*(?:public|protected|private)\s+"
    r"(?:(?:static|final|synchronized|abstract|default|native)\s+)*"
    r"([\w<>\[\]?,\s]+?)\s+(\w+)\s*\(",
    re.MULTILINE,
)


def _parse_with_regex(source: str, rel_path: str) -> Optional[ClassNode]:
    pkg_m = _RE_PACKAGE.search(source)
    package = pkg_m.group(1) if pkg_m else ""
    imports = _RE_IMPORT.findall(source)

    cls_m = _RE_CLASS.search(source)
    if not cls_m:
        return None

    kind = cls_m.group(1)
    class_name = cls_m.group(2)
    extends_raw = (cls_m.group(3) or "").strip() or None
    impl_raw = (cls_m.group(4) or "").strip()
    implements = [i.strip() for i in impl_raw.split(",")] if impl_raw else []
    is_interface = kind == "interface"
    class_pos = cls_m.start()

    # Annotations before class declaration
    annotations: list[AnnotationInfo] = [
        AnnotationInfo(name=m.group(1), attributes={})
        for m in _RE_ANNOTATION.finditer(source[:class_pos])
    ]

    node_type = _detect_node_type(annotations, class_name, extends_raw, implements, is_interface)

    # Base path via regex (reliable even when javalang unavailable)
    base_path: Optional[str] = None
    table_name: Optional[str] = None
    bp_m = re.search(
        r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?"([^"]+)"',
        source[:class_pos + 300],
    )
    if bp_m:
        base_path = bp_m.group(1)
    tn_m = re.search(r'@Table\s*\(\s*name\s*=\s*"([^"]+)"', source)
    if tn_m:
        table_name = tn_m.group(1)
    if node_type == NodeType.ENTITY and table_name is None:
        table_name = _to_snake_case(class_name)

    # Fields (best-effort)
    fields: list[FieldInfo] = []
    ann_stack: list[str] = []
    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("@"):
            ann_stack.append(stripped.lstrip("@").split("(")[0])
            continue
        fm = _RE_FIELD.match(stripped)
        if fm:
            ftype, fname = fm.group(1), fm.group(2)
            is_injected = "Autowired" in ann_stack or "Inject" in ann_stack
            is_id = "Id" in ann_stack
            rel = next((a for a in ann_stack if a in
                        {"OneToMany", "ManyToOne", "ManyToMany", "OneToOne"}), None)
            fields.append(FieldInfo(
                name=fname, type=ftype,
                is_injected=is_injected, is_id=is_id, relationship=rel,
            ))
            ann_stack = []
        elif stripped and not stripped.startswith("//"):
            ann_stack = []

    # Methods — relaxed regex, no parameter parsing (avoids annotated-param failures)
    methods: list[MethodInfo] = []
    seen: set[str] = set()
    for mm in _RE_METHOD.finditer(source):
        mname = mm.group(2)
        if mname in _JAVA_KEYWORDS or mname in seen:
            continue
        seen.add(mname)
        ret = mm.group(1).strip()
        methods.append(MethodInfo(name=mname, return_type=ret))

    # Endpoint + listener annotation enrichment via line scan
    _attach_endpoint_annotations(source, base_path, methods)

    return ClassNode(
        name=class_name, package=package, file_path=rel_path,
        node_type=node_type, annotations=annotations,
        fields=fields, methods=methods,
        extends=extends_raw, implements=implements, imports=imports,
        table_name=table_name, base_path=base_path,
        is_interface=is_interface,
        parse_error="regex-fallback",
    )


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def parse_java_file(file_path: str, project_root: str) -> Optional[ClassNode]:
    path = Path(file_path)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Cannot read %s: %s", file_path, exc)
        return None

    if not source.strip():
        return None

    rel_path = str(path.relative_to(project_root))
    node: Optional[ClassNode] = None

    if HAS_JAVALANG:
        node = _parse_with_javalang(source, file_path, rel_path)

    if node is None:
        log.debug("Falling back to regex parser for %s", rel_path)
        node = _parse_with_regex(source, rel_path)

    return node


def find_java_files(project_root: str) -> list[str]:
    root = Path(project_root)
    src_main = root / "src" / "main" / "java"
    search_root = src_main if src_main.exists() else root
    return [
        str(p) for p in search_root.rglob("*.java")
        if not any(part.startswith(".") for part in p.parts)
        and "target" not in p.parts
        and "build"  not in p.parts
    ]


def _to_snake_case(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()