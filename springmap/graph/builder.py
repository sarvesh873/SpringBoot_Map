from pathlib import Path
from springmap.graph.models import ProjectGraph
from springmap.parser.java_parser import JavaParser
from springmap.parser.config_parser import ConfigParser
from springmap.parser.proto_parser import ProtoParser
from springmap.parser.openapi_parser import OpenAPIParser

class GraphBuilder:
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)
        self.java_parser = JavaParser()
        self.config_parser = ConfigParser()
        self.proto_parser = ProtoParser()
        self.openapi_parser = OpenAPIParser()

    def build(self) -> ProjectGraph:
        config_node = self.config_parser.parse(self.project_path)
        graph = ProjectGraph(
            project_name=self.project_path.name,
            base_package="com.central",
            config=config_node
        )

        # 1. Parse Java Files
        for java_file in self.project_path.rglob("*.java"):
            node = self.java_parser.parse_file(java_file)
            if node: graph.classes[node.name] = node

        # 2. Parse Protobuf Files
        for proto_file in self.project_path.rglob("*.proto"):
            nodes = self.proto_parser.parse_file(proto_file)
            for node in nodes: graph.classes[node.name] = node

        # 3. Parse OpenAPI Specs
        for yaml_file in self.project_path.rglob("*.yaml"):
            node = self.openapi_parser.parse_file(yaml_file)
            if node: graph.classes[node.name] = node
        for yml_file in self.project_path.rglob("*.yml"):
            node = self.openapi_parser.parse_file(yml_file)
            if node: graph.classes[node.name] = node

        # Resolve Dependents
        for cls_name, cls_node in graph.classes.items():
            for dep_name in cls_node.dependencies:
                if dep_name in graph.classes:
                    graph.classes[dep_name].dependents.append(cls_name)

        return graph