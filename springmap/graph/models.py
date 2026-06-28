from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from datetime import datetime
from enum import Enum

class NodeType(Enum):
    CONTROLLER = "controller"
    SERVICE = "service"
    REPOSITORY = "repository"
    ENTITY = "entity"
    GRPC_SERVICE = "grpc_service"   # NEW
    OPENAPI_SPEC = "openapi_spec"   # NEW
    OTHER = "other"

@dataclass
class MethodInfo:
    name: str
    signature: str
    return_type: str
    parameters: List[Any] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)
    is_endpoint: bool = False
    http_method: Optional[str] = None
    http_path: Optional[str] = None
    request_body_type: Optional[str] = None
    is_transactional: bool = False
    is_async: bool = False
    is_scheduled: bool = False

@dataclass
class ClassNode:
    name: str
    file_path: str
    node_type: NodeType
    annotations: List[Any] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    dependents: List[str] = field(default_factory=list)
    methods: List[MethodInfo] = field(default_factory=list)
    endpoints: List[MethodInfo] = field(default_factory=list)
    extends: Optional[str] = None
    implements: List[str] = field(default_factory=list)
    base_path: Optional[str] = None
    table_name: Optional[str] = None
    fields: List[Any] = field(default_factory=list)

@dataclass
class ConfigNode:
    server_port: str = "8080"
    context_path: str = ""
    datasource_url: str = ""
    datasource_driver: str = ""
    jpa_ddl_auto: str = ""
    jpa_show_sql: bool = False
    active_profiles: List[str] = field(default_factory=list)
    custom_props: Dict[str, str] = field(default_factory=dict)

@dataclass
class ProjectGraph:
    project_name: str
    base_package: str
    config: ConfigNode
    classes: Dict[str, ClassNode] = field(default_factory=dict)
    java_version: str = "17"
    spring_boot_version: str = "3.1.0"
    generated_at: str = str(datetime.now())
    maven_dependencies: List[str] = field(default_factory=list)

    @property
    def controllers(self): return [c for c in self.classes.values() if c.node_type == NodeType.CONTROLLER]
    @property
    def services(self): return [c for c in self.classes.values() if c.node_type == NodeType.SERVICE]
    @property
    def repositories(self): return [c for c in self.classes.values() if c.node_type == NodeType.REPOSITORY]
    @property
    def entities(self): return [c for c in self.classes.values() if c.node_type == NodeType.ENTITY]
    @property
    def all_endpoints(self):
        endpoints = []
        for c in self.controllers:
            for m in c.endpoints:
                endpoints.append((c, m))
        return endpoints