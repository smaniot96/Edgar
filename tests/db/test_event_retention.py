"""Every event_type the production code writes has a retention class (db/README.md)."""

import ast
from collections.abc import Iterator
from pathlib import Path

from db.postgres.models.event_logs import EVENT_RETENTION

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DIRS = ("apps", "core", "db", "tools")


def _production_trees() -> list[ast.Module]:
    return [
        ast.parse(path.read_text(encoding="utf-8"))
        for top in PRODUCTION_DIRS
        for path in (ROOT / top).rglob("*.py")
        if "tests" not in path.parts
    ]


def _event_type_expressions(tree: ast.Module) -> Iterator[ast.expr]:
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "event_type":
            yield node.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "event_type":
                    yield value


def _written_event_types() -> set[str]:
    trees = _production_trees()
    constants = {
        target.id: node.value.value
        for tree in trees
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    written: set[str] = set()
    for tree in trees:
        for expression in _event_type_expressions(tree):
            # A subscript such as `event["event_type"]` only forwards a telemetry row whose
            # literal type is already found where that row is built.
            if isinstance(expression, ast.Constant):
                written.add(expression.value)
            elif isinstance(expression, ast.Name):
                written.add(constants[expression.id])
    return written


def test_every_written_event_type_has_a_retention_class():
    assert _written_event_types() == set(EVENT_RETENTION)
