"""FE3: typed relocation descriptors for ``emitVarStore``.

Mirror of ``ts/tests/test_reloc_descriptors.ts``.

``AddrReloc(addr)`` and ``TriggerFieldReloc(trigger_id, offset=0)`` describe
the two relocation shapes the HIR wire knows.  The descriptors normalise to
the legacy ``{"addr": ...}`` / ``{"trigger_id": ..., "offset": ...}`` objects at
the ``emitVarStore`` boundary, so HIR JSON and static cost stay byte-identical.
The descriptors are frozen dataclasses: a mixed or unknown shape is
unrepresentable, while a foreign object is rejected eagerly with ``EUDSL-TYPE``.
Numeric validation keeps the same messages and timing as the legacy form.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from typing import Any, Callable

from py import (
    AddrReloc,
    SetTo,
    Trigger,
    TriggerFieldReloc,
    Var,
    build_module,
)
from py.hir import emitVarStore
from py.view import EudslError

ADDR = 0x6509B0
UNALIGNED_ADDR = 0x6509B2
TRIGGER_OFFSET = 20
UNALIGNED_OFFSET = 14


def _module_json(target_for: Callable[[Trigger], Any]) -> str:
    def build() -> None:
        trigger = Trigger(conds=[], acts=[])
        var = Var(3)
        emitVarStore(var.id, target_for(trigger), SetTo)

    return build_module(build).to_json()


def _varstore_node(module: str) -> dict[str, Any]:
    stmts = json.loads(module)["module"]["stmts"]
    return next(stmt for stmt in stmts if stmt["kind"] == "varstore")


def _error(target_for: Callable[[Trigger], Any]) -> tuple[str, str]:
    caught: list[EudslError] = []

    def build() -> None:
        trigger = Trigger(conds=[], acts=[])
        var = Var(3)
        try:
            emitVarStore(var.id, target_for(trigger), SetTo)
        except EudslError as exc:
            caught.append(exc)

    build_module(build)
    assert len(caught) == 1
    return caught[0].code, str(caught[0])


class DescriptorHirEquivalenceTests(unittest.TestCase):
    def test_addr_reloc_matches_the_legacy_literal(self) -> None:
        legacy = _module_json(lambda _trigger: {"addr": ADDR})
        descriptor = _module_json(lambda _trigger: AddrReloc(ADDR))

        self.assertEqual(descriptor, legacy)
        node = _varstore_node(descriptor)
        self.assertEqual(node["dst_addr"], ADDR)
        self.assertNotIn("dst_trigger", node)
        self.assertNotIn("dst_offset", node)

    def test_trigger_field_reloc_matches_the_legacy_literal(self) -> None:
        legacy = _module_json(
            lambda trigger: {
                "trigger_id": trigger.id,
                "offset": TRIGGER_OFFSET,
            }
        )
        descriptor = _module_json(
            lambda trigger: TriggerFieldReloc(trigger.id, TRIGGER_OFFSET)
        )

        self.assertEqual(descriptor, legacy)
        node = _varstore_node(descriptor)
        self.assertEqual(node["dst_trigger"], 0)
        self.assertEqual(node["dst_offset"], TRIGGER_OFFSET)
        self.assertNotIn("dst_addr", node)

    def test_trigger_field_reloc_defaults_offset_to_zero(self) -> None:
        legacy = _module_json(lambda trigger: {"trigger_id": trigger.id})
        descriptor = _module_json(lambda trigger: TriggerFieldReloc(trigger.id))

        self.assertEqual(descriptor, legacy)
        self.assertEqual(_varstore_node(descriptor)["dst_offset"], 0)


class DescriptorValidationTests(unittest.TestCase):
    def test_unaligned_addr_matches_the_legacy_error(self) -> None:
        legacy = _error(lambda _trigger: {"addr": UNALIGNED_ADDR})
        descriptor = _error(lambda _trigger: AddrReloc(UNALIGNED_ADDR))

        self.assertEqual(descriptor, legacy)
        self.assertEqual(descriptor[0], "EUDSL-BOUNDS")
        self.assertEqual(
            descriptor[1],
            "EUDSL-BOUNDS: VarStore address 0x6509b2 is not 4-byte aligned",
        )

    def test_non_integer_addr_matches_the_legacy_error(self) -> None:
        legacy = _error(lambda _trigger: {"addr": "6291456"})
        descriptor = _error(lambda _trigger: AddrReloc("6291456"))  # type: ignore[arg-type]

        self.assertEqual(descriptor, legacy)
        self.assertEqual(descriptor[0], "EUDSL-TYPE")

    def test_unaligned_offset_matches_the_legacy_error(self) -> None:
        legacy = _error(
            lambda trigger: {
                "trigger_id": trigger.id,
                "offset": UNALIGNED_OFFSET,
            }
        )
        descriptor = _error(
            lambda trigger: TriggerFieldReloc(trigger.id, UNALIGNED_OFFSET)
        )

        self.assertEqual(descriptor, legacy)
        self.assertEqual(
            descriptor[1],
            "EUDSL-BOUNDS: VarStore offset 14 is not a multiple of 4",
        )

    def test_non_integer_trigger_id_matches_the_legacy_error(self) -> None:
        legacy = _error(lambda trigger: {"trigger_id": trigger.id + 0.5})
        descriptor = _error(
            lambda trigger: TriggerFieldReloc(trigger.id + 0.5)  # type: ignore[arg-type]
        )

        self.assertEqual(descriptor, legacy)
        self.assertEqual(descriptor[0], "EUDSL-TYPE")

    def test_foreign_target_is_rejected_eagerly(self) -> None:
        code, message = _error(lambda _trigger: object())

        self.assertEqual(code, "EUDSL-TYPE")
        self.assertIn("VarStore target", message)

    def test_mixed_shape_is_unrepresentable(self) -> None:
        with self.assertRaises(TypeError):
            AddrReloc(addr=ADDR, trigger_id=0)  # type: ignore[call-arg]

        with self.assertRaises(TypeError):
            TriggerFieldReloc(0, TRIGGER_OFFSET, 4)  # type: ignore[call-arg]

    def test_legacy_mapping_is_not_reinterpreted_as_a_descriptor(self) -> None:
        # A plain mapping stays on the legacy path, including an unrelated
        # `kind` key: the descriptor constructors are the only typed path.
        legacy = _module_json(lambda _trigger: {"addr": ADDR})
        shaped = _module_json(
            lambda _trigger: {"kind": "trigger", "addr": ADDR}
        )

        self.assertEqual(shaped, legacy)

    def test_production_modules_and_examples_use_descriptors(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        files = sorted((repo_root / "py").glob("*.py")) + sorted(
            (repo_root / "examples" / "py").glob("*.py")
        )
        raw_calls: list[str] = []
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                if func_name != "emitVarStore":
                    continue
                target_arg = node.args[1] if len(node.args) >= 2 else next(
                    (kw.value for kw in node.keywords if kw.arg == "target"),
                    None,
                )
                if isinstance(target_arg, ast.Dict):
                    raw_calls.append(f"{path.relative_to(repo_root)}:{node.lineno}")
        self.assertEqual(raw_calls, [])


if __name__ == "__main__":
    unittest.main()
