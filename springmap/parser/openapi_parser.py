import re
from pathlib import Path
from springmap.graph.models import ClassNode, NodeType, MethodInfo

class OpenAPIParser:
    def __init__(self):
        self.path_regex = re.compile(r'^\s\s(/[\w/{}-]+):', re.MULTILINE)
        self.method_regex = re.compile(r'^\s{4}(get|post|put|delete|patch):', re.MULTILINE | re.IGNORECASE)
        self.operation_id_regex = re.compile(r'operationId:\s*(\w+)')

    def parse_file(self, file_path: Path) -> ClassNode | None:
        content = file_path.read_text(encoding='utf-8')
        
        if 'openapi:' not in content and 'swagger:' not in content:
            return None
            
        node = ClassNode(
            name=f"OpenAPI_Spec_{file_path.stem}",
            file_path=str(file_path),
            node_type=NodeType.OPENAPI_SPEC,
        )

        # Split content into path blocks
        path_blocks = self.path_regex.split(content)
        
        # path_blocks[0] is header, the rest are [path1, content1, path2, content2...]
        for i in range(1, len(path_blocks), 2):
            path = path_blocks[i]
            block = path_blocks[i+1]
            
            for method_match in self.method_regex.finditer(block):
                http_method = method_match.group(1).upper()
                
                # Try to find operationId near this method
                op_match = self.operation_id_regex.search(block[method_match.end():])
                method_name = op_match.group(1) if op_match else f"{http_method.lower()}Endpoint"
                
                method_info = MethodInfo(
                    name=method_name,
                    signature=f"{http_method} {path}",
                    return_type="JSON",
                    is_endpoint=True,
                    http_method=http_method,
                    http_path=path
                )
                node.endpoints.append(method_info)

        return node if node.endpoints else None