"""YAML loading that refuses duplicate keys and reports where a file broke."""

from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError

from rc.errors import RcError


class StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in seen
                except TypeError:
                    continue
                if duplicate:
                    raise ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_text(text: str, origin: str) -> Any:
    try:
        return yaml.load(text, Loader=StrictLoader)  # noqa: S506 - StrictLoader is a SafeLoader
    except yaml.YAMLError as exc:
        raise RcError(f"{origin}: {exc}") from None


def load_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RcError(f"cannot read {path}: {exc.strerror}") from None
    return load_text(text, str(path))
