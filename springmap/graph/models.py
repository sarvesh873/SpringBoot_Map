"""
Data models for the SpringMap knowledge graph.
Every parsed element in the Spring Boot project becomes a node here.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NodeType(str, Enum):
    CONTROLLER = "controller"
    SERVICE = "service"
    REPOSITORY = "repository"
    ENTITY = "entity"
    COMPONENT = "component"
    CONFIGURATION = "configuration"
    DTO = "dto"
    EXCEPTION = "exception"
    UTIL = "util"
    MAIN = "main"
    INTERFACE = "interface"
    OPENAPI = "openapi"    # virtual node sourced from OpenAPI/Swagger YAML
    GRPC = "grpc"          # virtual node sourced from .proto file
    UNKNOWN = "unknown"


# Annotations that definitively identify a node's type
ANNOTATION_TO_TYPE: dict[str, NodeType] = {
    "RestController": NodeType.CONTROLLER,
    "Controller": NodeType.CONTROLLER,
    "Service": NodeType.SERVICE,
    "Repository": NodeType.REPOSITORY,
    "Entity": NodeType.ENTITY,
    "Component": NodeType.COMPONENT,
    "Configuration": NodeType.CONFIGURATION,
    "SpringBootApplication": NodeType.MAIN,
    "ControllerAdvice": NodeType.EXCEPTION,
    "RestControllerAdvice": NodeType.EXCEPTION,
    "FeignClient": NodeType.OPENAPI,          # Feign clients define API contracts
}

HTTP_MAPPING_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "REQUEST",
}

# Single source of truth for verb categorization — imported by java_parser,
# query/engine, exporters, and cli so "what counts as REST vs a listener"
# never drifts between modules.
HTTP_VERBS: frozenset[str] = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "REQUEST"})
LISTENER_VERBS: frozenset[str] = frozenset({
    "KAFKA", "RABBIT", "SQS", "JMS", "EVENT", "SCHEDULED", "STREAM",
})
RPC_VERB = "RPC"


def categorize_verb(verb: Optional[str]) -> str:
    """Classify an http_method value into 'rest' | 'grpc' | 'listener' | 'other'."""
    v = (verb or "").upper()
    if v in HTTP_VERBS:
        return "rest"
    if v == RPC_VERB:
        return "grpc"
    if v in LISTENER_VERBS:
        return "listener"
    return "other"


@dataclass
class AnnotationInfo:
    name: str
    attributes: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        if not self.attributes:
            return f"@{self.name}"
        attrs = ", ".join(f'{k}="{v}"' for k, v in self.attributes.items())
        return f"@{self.name}({attrs})"

    def to_dict(self) -> dict:
        return {"name": self.name, "attributes": self.attributes}


@dataclass
class ParamInfo:
    type: str
    name: str
    annotations: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.type} {self.name}"


@dataclass
class MethodInfo:
    name: str
    return_type: str
    parameters: list[ParamInfo] = field(default_factory=list)
    annotations: list[AnnotationInfo] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)   # "ClassName.method()" strings
    is_transactional: bool = False
    is_async: bool = False
    is_scheduled: bool = False
    http_method: Optional[str] = None   # GET / POST / PUT / DELETE / PATCH
    http_path: Optional[str] = None
    request_body_type: Optional[str] = None
    line: int = 0

    @property
    def signature(self) -> str:
        params = ", ".join(str(p) for p in self.parameters)
        return f"{self.return_type} {self.name}({params})"

    @property
    def is_endpoint(self) -> bool:
        return self.http_method is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "return_type": self.return_type,
            "parameters": [{"type": p.type, "name": p.name, "annotations": p.annotations}
                           for p in self.parameters],
            "annotations": [a.to_dict() for a in self.annotations],
            "calls": self.calls,
            "is_transactional": self.is_transactional,
            "is_async": self.is_async,
            "is_scheduled": self.is_scheduled,
            "http_method": self.http_method,
            "http_path": self.http_path,
            "request_body_type": self.request_body_type,
            "line": self.line,
            "signature": self.signature,
        }


@dataclass
class FieldInfo:
    name: str
    type: str
    annotations: list[AnnotationInfo] = field(default_factory=list)
    is_injected: bool = False      # @Autowired or constructor injection
    is_id: bool = False            # @Id
    column_name: Optional[str] = None
    relationship: Optional[str] = None   # OneToMany / ManyToOne / etc.
    config_key: Optional[str] = None     # for @Value("${...}")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "is_injected": self.is_injected,
            "is_id": self.is_id,
            "column_name": self.column_name,
            "relationship": self.relationship,
            "config_key": self.config_key,
        }


@dataclass
class ClassNode:
    name: str
    package: str
    file_path: str          # relative to project root
    node_type: NodeType
    annotations: list[AnnotationInfo] = field(default_factory=list)
    fields: list[FieldInfo] = field(default_factory=list)
    methods: list[MethodInfo] = field(default_factory=list)
    extends: Optional[str] = None
    implements: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    table_name: Optional[str] = None     # for @Entity
    base_path: Optional[str] = None      # for class-level @RequestMapping
    is_interface: bool = False
    parse_error: Optional[str] = None    # set if AST parse failed, used regex
    # Resolved by GraphBuilder post-parse (not available in raw parser output)
    dependencies: list[str] = field(default_factory=list)   # class names this node depends on
    dependents: list[str] = field(default_factory=list)     # class names that depend on this node
    source: str = "java"                 # "java" | "openapi" | "proto"

    @property
    def full_name(self) -> str:
        return f"{self.package}.{self.name}" if self.package else self.name

    @property
    def injected_types(self) -> list[str]:
        """Return the type names of all injected dependencies."""
        return [f.type for f in self.fields if f.is_injected]

    @property
    def endpoints(self) -> list[MethodInfo]:
        return [m for m in self.methods if m.is_endpoint]

    @property
    def transactional_methods(self) -> list[MethodInfo]:
        return [m for m in self.methods if m.is_transactional]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "full_name": self.full_name,
            "package": self.package,
            "file_path": self.file_path,
            "node_type": self.node_type.value,
            "source": self.source,
            "annotations": [a.to_dict() for a in self.annotations],
            "fields": [f.to_dict() for f in self.fields],
            "methods": [m.to_dict() for m in self.methods],
            "extends": self.extends,
            "implements": self.implements,
            "table_name": self.table_name,
            "base_path": self.base_path,
            "is_interface": self.is_interface,
            "injected_types": self.injected_types,
            "dependencies": self.dependencies,
            "dependents": self.dependents,
            "parse_error": self.parse_error,
        }


@dataclass
class ProjectConfig:
    """Extracted from application.yml / application.properties."""
    server_port: str = "8080"
    context_path: str = ""
    active_profiles: list[str] = field(default_factory=list)
    datasource_url: Optional[str] = None
    datasource_driver: Optional[str] = None
    jpa_ddl_auto: Optional[str] = None
    jpa_show_sql: bool = False
    custom_props: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "server_port": self.server_port,
            "context_path": self.context_path,
            "active_profiles": self.active_profiles,
            "datasource_url": self.datasource_url,
            "datasource_driver": self.datasource_driver,
            "jpa_ddl_auto": self.jpa_ddl_auto,
            "jpa_show_sql": self.jpa_show_sql,
            "custom_props": self.custom_props,
        }


@dataclass
class ProjectGraph:
    project_name: str = "Unknown"
    base_package: str = ""
    java_version: str = ""
    spring_boot_version: str = ""
    maven_dependencies: list[str] = field(default_factory=list)
    classes: dict[str, ClassNode] = field(default_factory=dict)  # name → ClassNode
    config: ProjectConfig = field(default_factory=ProjectConfig)
    generated_at: str = ""
    source_root: str = ""

    def get_by_type(self, node_type: NodeType) -> list[ClassNode]:
        return sorted(
            [c for c in self.classes.values() if c.node_type == node_type],
            key=lambda c: c.name,
        )

    @property
    def controllers(self) -> list[ClassNode]:
        return self.get_by_type(NodeType.CONTROLLER)

    @property
    def services(self) -> list[ClassNode]:
        return self.get_by_type(NodeType.SERVICE)

    @property
    def repositories(self) -> list[ClassNode]:
        return self.get_by_type(NodeType.REPOSITORY)

    @property
    def entities(self) -> list[ClassNode]:
        return self.get_by_type(NodeType.ENTITY)

    @property
    def openapi_nodes(self) -> list[ClassNode]:
        return self.get_by_type(NodeType.OPENAPI)

    @property
    def grpc_services(self) -> list[ClassNode]:
        return self.get_by_type(NodeType.GRPC)

    @property
    def all_endpoints(self) -> list[tuple[ClassNode, MethodInfo]]:
        """
        All REST-style endpoints (GET/POST/PUT/DELETE/PATCH/REQUEST) from BOTH
        Java @RestController classes AND OpenAPI-spec-derived virtual nodes.

        BUG FIX: previously this only scanned self.controllers, so any endpoint
        defined purely in an openapi.yaml / swagger.yaml (gateway routes, generated
        contracts) never appeared in GRAPH.md or `springmap stats`.
        """
        result: list[tuple[ClassNode, MethodInfo]] = []
        for cls in self.classes.values():
            if cls.node_type not in (NodeType.CONTROLLER, NodeType.OPENAPI):
                continue
            for method in cls.methods:
                if method.http_method and method.http_method.upper() in HTTP_VERBS:
                    result.append((cls, method))
        return result

    @property
    def all_grpc_methods(self) -> list[tuple[ClassNode, MethodInfo]]:
        """All gRPC RPC methods from .proto-derived service nodes."""
        result: list[tuple[ClassNode, MethodInfo]] = []
        for cls in self.grpc_services:
            for method in cls.methods:
                result.append((cls, method))
        return result

    @property
    def all_listeners(self) -> list[tuple[ClassNode, MethodInfo]]:
        """
        All Kafka / RabbitMQ / SQS / JMS / @EventListener / @Scheduled methods,
        scanned across EVERY class (not just NodeType.COMPONENT) since listener
        methods commonly live inside @Service classes too.
        """
        result: list[tuple[ClassNode, MethodInfo]] = []
        for cls in self.classes.values():
            for method in cls.methods:
                if method.http_method and method.http_method.upper() in LISTENER_VERBS:
                    result.append((cls, method))
        return result

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "base_package": self.base_package,
            "java_version": self.java_version,
            "spring_boot_version": self.spring_boot_version,
            "maven_dependencies": self.maven_dependencies,
            "classes": {name: cls.to_dict() for name, cls in self.classes.items()},
            "config": self.config.to_dict(),
            "generated_at": self.generated_at,
            "source_root": self.source_root,
        }