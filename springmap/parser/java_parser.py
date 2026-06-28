"""
Java source file parser.

Primary strategy: javalang AST parser (handles Java 8 syntax cleanly).
Fallback strategy: regex-based extraction when javalang fails (records, text blocks,
sealed classes, etc. introduced in Java 14+).

For Spring Boot projects the critical classes — controllers, services, repositories,
entities — almost always use classic class syntax that javalang handles fine.
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
# Annotation helpers
# ─────────────────────────────────────────────

def _ann_attributes(annotation) -> dict[str, str]:
    """Extract annotation attributes from a javalang Annotation node."""
    el = annotation.element
    if el is None:
        return {}

    # Single literal value: @Annotation("value")
    if isinstance(el, javalang.tree.Literal):
        return {"value": el.value.strip('"').strip("'")}

    # Single member reference: @Annotation(Method.GET)
    if isinstance(el, javalang.tree.MemberReference):
        return {"value": str(el.member)}

    # List of member-value pairs: @Annotation(key="v", key2="v2")
    if isinstance(el, list):
        result: dict[str, str] = {}
        for item in el:
            if isinstance(item, javalang.tree.MemberValuePair):
                val = item.value
                if isinstance(val, javalang.tree.Literal):
                    result[item.name] = val.value.strip('"').strip("'")
                elif isinstance(val, javalang.tree.MemberReference):
                    result[item.name] = str(val.member)
                elif isinstance(val, javalang.tree.ArrayInitializer):
                    # e.g. @RequestMapping(value={"/a", "/b"})
                    values = []
                    for v in (val.initializers or []):
                        if isinstance(v, javalang.tree.Literal):
                            values.append(v.value.strip('"').strip("'"))
                    result[item.name] = values[0] if len(values) == 1 else str(values)
                else:
                    result[item.name] = str(val)
        return result

    return {}


def _parse_annotations(raw_annotations: list) -> list[AnnotationInfo]:
    infos = []
    for ann in (raw_annotations or []):
        try:
            attrs = _ann_attributes(ann)
        except Exception:
            attrs = {}
        infos.append(AnnotationInfo(name=ann.name, attributes=attrs))
    return infos


def _resolve_type_name(ref_type) -> str:
    """Get a flat string type name from a javalang ReferenceType or BasicType."""
    if ref_type is None:
        return "void"
    if isinstance(ref_type, javalang.tree.BasicType):
        return ref_type.name
    if isinstance(ref_type, javalang.tree.ReferenceType):
        name = ref_type.name
        if ref_type.arguments:
            args = ", ".join(_resolve_type_name(a.type) for a in ref_type.arguments
                            if hasattr(a, "type") and a.type is not None)
            return f"{name}<{args}>"
        return name
    return str(ref_type)


# ─────────────────────────────────────────────
# Endpoint path resolution
# ─────────────────────────────────────────────

def _get_path(ann_attrs: dict[str, str]) -> Optional[str]:
    """Pull the HTTP path from annotation attributes (value or path key)."""
    return ann_attrs.get("value") or ann_attrs.get("path")


def _combine_paths(base: Optional[str], local: Optional[str]) -> str:
    base = (base or "").rstrip("/")
    local = (local or "")
    if not local.startswith("/"):
        local = "/" + local
    return base + local


# ─────────────────────────────────────────────
# Method call extraction
# ─────────────────────────────────────────────

def _extract_calls(method_decl, field_type_map: dict[str, str]) -> list[str]:
    """Walk a method body and collect ClassName.method() call strings."""
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
            # Skip bare method calls (same-class calls) to reduce noise
    except Exception:
        pass
    return list(dict.fromkeys(calls))  # deduplicate, preserve order


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

    # Heuristics based on name / inheritance when annotations are missing
    name_lower = class_name.lower()
    if extends and "jparepository" in extends.lower():
        return NodeType.REPOSITORY
    if any("repository" in i.lower() for i in implements):
        return NodeType.REPOSITORY
    if name_lower.endswith("controller"):
        return NodeType.CONTROLLER
    if name_lower.endswith("service") or name_lower.endswith("serviceimpl"):
        return NodeType.SERVICE
    if name_lower.endswith("repository") or name_lower.endswith("repo"):
        return NodeType.REPOSITORY
    if name_lower.endswith("entity"):
        return NodeType.ENTITY
    if name_lower.endswith("dto") or name_lower.endswith("request") or name_lower.endswith("response"):
        return NodeType.DTO
    if name_lower.endswith("config") or name_lower.endswith("configuration"):
        return NodeType.CONFIGURATION
    if name_lower.endswith("exception") or name_lower.endswith("error"):
        return NodeType.EXCEPTION
    if name_lower.endswith("util") or name_lower.endswith("utils") or name_lower.endswith("helper"):
        return NodeType.UTIL

    return NodeType.UNKNOWN


# ─────────────────────────────────────────────
# AST-based parsing (javalang)
# ─────────────────────────────────────────────

def _parse_with_javalang(source: str, file_path: str, rel_path: str) -> Optional[ClassNode]:
    """Parse a Java file using javalang AST. Returns None on parse failure."""
    try:
        tree = javalang.parse.parse(source)
    except Exception as exc:
        log.debug("javalang failed on %s: %s", rel_path, exc)
        return None

    package = tree.package.name if tree.package else ""
    imports = [imp.path for imp in (tree.imports or [])]

    # Pick the first class/interface/enum in the file
    for type_decl in (tree.types or []):
        is_interface = isinstance(type_decl, javalang.tree.InterfaceDeclaration)
        is_enum = isinstance(type_decl, javalang.tree.EnumDeclaration)

        if not isinstance(type_decl, (
            javalang.tree.ClassDeclaration,
            javalang.tree.InterfaceDeclaration,
            javalang.tree.EnumDeclaration,
        )):
            continue

        class_name = type_decl.name
        annotations = _parse_annotations(type_decl.annotations)

        # Extends / implements
        extends: Optional[str] = None
        if hasattr(type_decl, "extends") and type_decl.extends:
            ext = type_decl.extends
            if isinstance(ext, list):
                extends = _resolve_type_name(ext[0]) if ext else None
            else:
                extends = _resolve_type_name(ext)

        implements: list[str] = []
        if hasattr(type_decl, "implements") and type_decl.implements:
            implements = [_resolve_type_name(i) for i in type_decl.implements]

        node_type = _detect_node_type(annotations, class_name, extends, implements, is_interface)

        # Class-level @RequestMapping base path
        base_path: Optional[str] = None
        table_name: Optional[str] = None
        for ann in annotations:
            if ann.name == "RequestMapping":
                base_path = _get_path(ann.attributes) or ""
            if ann.name == "Table":
                table_name = ann.attributes.get("name")

        if node_type == NodeType.ENTITY and table_name is None:
            table_name = _to_snake_case(class_name)

        # Fields
        field_type_map: dict[str, str] = {}   # fieldName → fieldType (for call resolution)
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
                field_type_map[decl.name] = raw_type
                # Also map lowercase first letter (common field naming convention)
                short = raw_type.split("<")[0]  # strip generics
                camel = short[0].lower() + short[1:] if short else short
                field_type_map[camel] = short

                fi = FieldInfo(
                    name=decl.name,
                    type=raw_type,
                    annotations=field_anns,
                    is_injected=is_injected,
                    is_id=is_id,
                    column_name=column_name,
                    relationship=relationship,
                    config_key=config_key,
                )
                fields.append(fi)

        # Constructor injection detection: look for constructors with single
        # argument that matches a known Spring stereotype
        for ctor in (getattr(type_decl, "constructors", None) or []):
            ctor_anns = {a.name for a in (ctor.annotations or [])}
            # Constructor injection if @Autowired or only one constructor
            for param in (ctor.parameters or []):
                param_type = _resolve_type_name(param.type)
                param_name = param.name
                # Mark as injected if the field exists and wasn't already marked
                for fi in fields:
                    if fi.name == param_name and fi.type == param_type:
                        fi.is_injected = True
                field_type_map[param_name] = param_type

        # Methods
        methods: list[MethodInfo] = []
        for method in (getattr(type_decl, "methods", None) or []):
            m_anns = _parse_annotations(method.annotations)
            m_ann_names = {a.name for a in m_anns}

            # HTTP endpoint info
            http_method: Optional[str] = None
            http_path: Optional[str] = None
            request_body_type: Optional[str] = None
            for ann in m_anns:
                if ann.name in HTTP_MAPPING_ANNOTATIONS:
                    http_method = HTTP_MAPPING_ANNOTATIONS[ann.name]
                    local_path = _get_path(ann.attributes) or ""
                    http_path = _combine_paths(base_path, local_path)
                    break

            # Parameters
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

            mi = MethodInfo(
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
            )
            methods.append(mi)

        return ClassNode(
            name=class_name,
            package=package,
            file_path=rel_path,
            node_type=node_type,
            annotations=annotations,
            fields=fields,
            methods=methods,
            extends=extends,
            implements=implements,
            imports=imports,
            table_name=table_name,
            base_path=base_path,
            is_interface=is_interface,
        )

    return None   # no class/interface found in file


# ─────────────────────────────────────────────
# Regex-based fallback parser
# ─────────────────────────────────────────────

_RE_PACKAGE = re.compile(r"package\s+([\w.]+)\s*;")
_RE_IMPORT = re.compile(r"import\s+([\w.]+)\s*;")
_RE_CLASS = re.compile(
    r"(?:@\w+(?:\([^)]*\))?\s*)*"
    r"(?:public\s+)?(?:abstract\s+)?(?:final\s+)?"
    r"(class|interface|enum|record)\s+(\w+)"
    r"(?:\s+extends\s+([\w<>, ]+?))?(?:\s+implements\s+([\w<>, ]+?))?\s*\{"
)
_RE_ANNOTATION = re.compile(r'@(\w+)(?:\(\s*(?:"([^"]*)"|(value\s*=\s*"([^"]*)"))\s*\))?')
_RE_METHOD = re.compile(
    r"(?:public|protected|private|static|final|\s)+"
    r"([\w<>\[\],\s]+?)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w,\s]+)?\s*\{"
)
_RE_FIELD = re.compile(
    r"(?:private|protected|public|final|static|\s)+"
    r"([\w<>\[\]]+)\s+(\w+)\s*(?:=\s*[^;]+)?\s*;"
)


def _parse_with_regex(source: str, rel_path: str) -> Optional[ClassNode]:
    """Regex-based extraction — used when javalang cannot parse the file."""
    pkg_match = _RE_PACKAGE.search(source)
    package = pkg_match.group(1) if pkg_match else ""
    imports = _RE_IMPORT.findall(source)

    # Collect all annotations before the class declaration
    raw_anns = _RE_ANNOTATION.findall(source[:2000])  # check first 2 KB
    annotations: list[AnnotationInfo] = []
    for ann_name, val1, _, val2 in raw_anns:
        value = val1 or val2
        attrs = {"value": value} if value else {}
        annotations.append(AnnotationInfo(name=ann_name, attributes=attrs))

    cls_match = _RE_CLASS.search(source)
    if not cls_match:
        return None

    kind = cls_match.group(1)           # class / interface / enum / record
    class_name = cls_match.group(2)
    extends = (cls_match.group(3) or "").strip() or None
    implements_raw = (cls_match.group(4) or "").strip()
    implements = [i.strip() for i in implements_raw.split(",")] if implements_raw else []
    is_interface = kind == "interface"

    # Only keep annotations that appeared before the class declaration
    class_pos = cls_match.start()
    ann_before: list[AnnotationInfo] = []
    for m in _RE_ANNOTATION.finditer(source[:class_pos]):
        ann_before.append(AnnotationInfo(name=m.group(1), attributes={}))
    if ann_before:
        annotations = ann_before

    node_type = _detect_node_type(annotations, class_name, extends, implements, is_interface)

    # Base path
    base_path: Optional[str] = None
    table_name: Optional[str] = None
    for ann in annotations:
        if ann.name == "RequestMapping":
            base_path = ann.attributes.get("value")
        if ann.name == "Table":
            table_name = ann.attributes.get("name")
    if node_type == NodeType.ENTITY and table_name is None:
        table_name = _to_snake_case(class_name)

    # Fields (best-effort, only private fields)
    fields: list[FieldInfo] = []
    field_type_map: dict[str, str] = {}
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
            field_type_map[fname] = ftype
            ann_stack = []
            continue
        if stripped and not stripped.startswith("//"):
            ann_stack = []

    # Methods (best-effort)
    methods: list[MethodInfo] = []
    for mm in _RE_METHOD.finditer(source):
        ret = mm.group(1).strip().split()[-1]  # last token = type
        mname = mm.group(2)
        if mname in {"if", "while", "for", "switch", "catch"}:
            continue
        params_raw = mm.group(3).strip()
        params: list[ParamInfo] = []
        if params_raw:
            for p in params_raw.split(","):
                parts = p.strip().split()
                if len(parts) >= 2:
                    params.append(ParamInfo(type=parts[-2], name=parts[-1]))
        methods.append(MethodInfo(
            name=mname, return_type=ret, parameters=params,
        ))

    # Endpoint detection from @*Mapping annotations in method regions
    mapping_re = re.compile(
        r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)'
        r'(?:\("([^"]+)"\))?\s*\n.*?(?:public|protected)\s+\S+\s+(\w+)\s*\(',
        re.DOTALL
    )
    for mm in mapping_re.finditer(source):
        ann_name = mm.group(1)
        path = mm.group(2) or ""
        method_name = mm.group(3)
        http_verb = HTTP_MAPPING_ANNOTATIONS.get(ann_name, "REQUEST")
        full_path = _combine_paths(base_path, path)
        # Update existing method info if found
        for mi in methods:
            if mi.name == method_name and mi.http_method is None:
                mi.http_method = http_verb
                mi.http_path = full_path
                break

    return ClassNode(
        name=class_name,
        package=package,
        file_path=rel_path,
        node_type=node_type,
        annotations=annotations,
        fields=fields,
        methods=methods,
        extends=extends,
        implements=implements,
        imports=imports,
        table_name=table_name,
        base_path=base_path,
        is_interface=is_interface,
        parse_error="regex-fallback",
    )


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def parse_java_file(file_path: str, project_root: str) -> Optional[ClassNode]:
    """
    Parse a single Java source file.

    Tries javalang first; falls back to regex if AST parsing fails.
    Returns None if the file is empty or no class is found.
    """
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
    """Find all .java files under src/main/java/. Falls back to whole tree."""
    root = Path(project_root)
    src_main = root / "src" / "main" / "java"
    search_root = src_main if src_main.exists() else root

    return [
        str(p) for p in search_root.rglob("*.java")
        if not any(part.startswith(".") for part in p.parts)
    ]


# ─────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────

def _to_snake_case(name: str) -> str:
    """UserAccount → user_account"""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()