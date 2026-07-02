"""
JSON exporter — serializes ProjectGraph to graph.json using SpringMapEncoder.

SpringMapEncoder lets us pass dataclass/model objects directly to json.dumps()
without manually calling .to_dict() at every call site — the encoder handles it.
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from springmap.graph.models import ProjectGraph


class SpringMapEncoder(json.JSONEncoder):
    """
    Custom JSON encoder that handles:
      - Objects with a .to_dict() method (ClassNode, MethodInfo, etc.)
      - Enum values  →  their .value string
      - set / frozenset  →  sorted list
      - pathlib.Path  →  str
    """

    def default(self, obj: Any) -> Any:
        if hasattr(obj, "to_dict") and callable(obj.to_dict):
            return obj.to_dict()
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (set, frozenset)):
            return sorted(obj)
        if isinstance(obj, Path):
            return str(obj)
        return super().default(obj)


def export_json(graph: ProjectGraph, out_dir: Path) -> Path:
    """Serialize the full ProjectGraph to graph.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "graph.json"
    # Use to_dict() for a clean, self-contained dict — encoder is a safety net
    out_path.write_text(
        json.dumps(graph.to_dict(), indent=2, cls=SpringMapEncoder),
        encoding="utf-8",
    )
    return out_path


def load_graph_json(out_dir: Path) -> dict | None:
    path = out_dir / "graph.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
