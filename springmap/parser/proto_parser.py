import re
from pathlib import Path
from springmap.graph.models import ClassNode, NodeType, MethodInfo

class ProtoParser:
    def __init__(self):
        # Extracts: service HoldService {
        self.service_regex = re.compile(r'service\s+(\w+)\s*\{')
        # Extracts: rpc PlaceHold (PlaceHoldRequestGRPC) returns (HoldResponseGRPC);
        self.rpc_regex = re.compile(r'rpc\s+(\w+)\s*\(([^)]+)\)\s*returns\s*\(([^)]+)\)')

    def parse_file(self, file_path: Path) -> list[ClassNode]:
        content = file_path.read_text(encoding='utf-8')
        nodes = []
        
        # A single proto file can have multiple services
        for service_match in self.service_regex.finditer(content):
            service_name = service_match.group(1)
            
            node = ClassNode(
                name=service_name,
                file_path=str(file_path),
                node_type=NodeType.GRPC_SERVICE,
            )
            
            # Find all RPCs in this file (simplified scoping)
            for rpc_match in self.rpc_regex.finditer(content):
                method_name = rpc_match.group(1)
                req_type = rpc_match.group(2)
                res_type = rpc_match.group(3)
                
                method_info = MethodInfo(
                    name=method_name,
                    signature=f"{res_type} {method_name}({req_type})",
                    return_type=res_type,
                    is_endpoint=True,
                    http_method="gRPC",
                    http_path=f"/{service_name}/{method_name}"
                )
                node.endpoints.append(method_info)
                
            nodes.append(node)
            
        return nodes