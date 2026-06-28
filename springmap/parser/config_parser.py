import re
from pathlib import Path
from springmap.graph.models import ConfigNode

class ConfigParser:
    def __init__(self):
        # Matches both properties and YAML formats (e.g., spring.datasource.url: jdbc:... or url=jdbc:...)
        self.port_regex = re.compile(r'port\s*[:=]\s*(\d+)')
        self.db_url_regex = re.compile(r'url\s*[:=]\s*([^\s]+)')
        self.db_driver_regex = re.compile(r'driver-class-name\s*[:=]\s*([^\s]+)')
        self.ddl_regex = re.compile(r'ddl-auto\s*[:=]\s*([^\s]+)')

    def parse(self, project_path: Path) -> ConfigNode:
        config = ConfigNode()
        
        # Look for application properties or yaml
        for ext in ['properties', 'yml', 'yaml']:
            for config_file in project_path.rglob(f"application.{ext}"):
                content = config_file.read_text(encoding='utf-8')
                
                if match := self.port_regex.search(content): config.server_port = match.group(1)
                if match := self.db_url_regex.search(content): config.datasource_url = match.group(1)
                if match := self.db_driver_regex.search(content): config.datasource_driver = match.group(1)
                if match := self.ddl_regex.search(content): config.jpa_ddl_auto = match.group(1)
                
        return config