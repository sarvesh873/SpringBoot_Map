import re
from pathlib import Path
from springmap.graph.models import ClassNode, NodeType, MethodInfo

class JavaParser:
    def __init__(self):
        self.class_regex = re.compile(r'public\s+(?:class|interface|record)\s+(\w+)')
        self.package_regex = re.compile(r'package\s+([\w\.]+);')
        self.dep_regex = re.compile(r'private\s+(?:final\s+)?([A-Z]\w+)\s+\w+;')
        self.method_regex = re.compile(r'(?:public|protected|private)\s+(?:[\w<>\[\]]+\s+)?([A-Z]\w*(?:<[^>]+>)?)\s+(\w+)\s*\(')
        
        # Spring specific annotations
        self.mapping_regex = re.compile(r'@(Get|Post|Put|Delete|Patch)Mapping(?:\(\s*["\']([^"\']+)["\'])?')
        self.transactional_regex = re.compile(r'@Transactional')
        self.kafka_listener_regex = re.compile(r'@KafkaListener\(.*topics\s*=\s*["\']([^"\']+)["\']')

    def determine_type(self, content: str) -> NodeType:
        if '@RestController' in content or '@Controller' in content: return NodeType.CONTROLLER
        if '@Service' in content: return NodeType.SERVICE
        if '@Repository' in content or 'extends JpaRepository' in content: return NodeType.REPOSITORY
        if '@Entity' in content: return NodeType.ENTITY
        return NodeType.OTHER

    def parse_file(self, file_path: Path) -> ClassNode | None:
        content = file_path.read_text(encoding='utf-8')
        class_match = self.class_regex.search(content)
        
        if not class_match:
            return None
            
        name = class_match.group(1)
        node_type = self.determine_type(content)
        
        node = ClassNode(
            name=name,
            file_path=str(file_path),
            node_type=node_type,
            dependencies=self.dep_regex.findall(content)
        )

        # Extract Methods and Endpoints
        for match in self.method_regex.finditer(content):
            ret_type, meth_name = match.groups()
            
            # Look backwards slightly from the method definition to find annotations
            method_context_start = max(0, match.start() - 200)
            method_context = content[method_context_start:match.start()]
            
            mapping_match = self.mapping_regex.search(method_context)
            is_tx = bool(self.transactional_regex.search(method_context) or self.transactional_regex.search(content))
            
            method_info = MethodInfo(
                name=meth_name, 
                signature=f"{ret_type} {meth_name}()", 
                return_type=ret_type,
                is_transactional=is_tx
            )

            # If it's a controller endpoint (mapped directly OR returns ResponseEntity)
            if mapping_match:
                method_info.is_endpoint = True
                method_info.http_method = mapping_match.group(1).upper()
                method_info.http_path = mapping_match.group(2) or "/"
                node.endpoints.append(method_info)
            elif node_type == NodeType.CONTROLLER and "ResponseEntity" in ret_type:
                # NEW: Catch interface-driven endpoints
                method_info.is_endpoint = True
                method_info.http_method = "API_INTERFACE"
                method_info.http_path = "Delegated to OpenAPI spec"
                node.endpoints.append(method_info)
            else:
                node.methods.append(method_info)
                
            # If it's a Kafka listener, add it as a "custom" endpoint so the AI knows data enters here
            if kafka_match := self.kafka_listener_regex.search(method_context):
                method_info.is_endpoint = True
                method_info.http_method = "KAFKA_CONSUMER"
                method_info.http_path = kafka_match.group(1)
                node.endpoints.append(method_info)

        return node