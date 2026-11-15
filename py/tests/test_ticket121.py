"""Ticket #121: validate ``VarStore`` targets before queueing HIR.

Mirror of ``ts/tests/test_ticket121.ts``.

A handler for a trigger field is a whole-dword write.  A malformed
``{"trigger_id": ..., "offset": ...}`` target used to be lowered as a mode-1
4-byte field relocation even when ``offset`` was 14, silently writing
``unit``/``comparison``/``condtype``; floats and numeric strings were coerced
by ``int()`` before validation.  ``emitVarStore`` now validates the target
eagerly at the call, before any HIR node is queued:

* ``addr`` is a plain integer (the #111 alignment guard still applies);
* ``trigger_id`` and ``offset`` are plain integers, and ``offset % 4 == 0``.

The failures are eager ``EUDSL-TYPE`` / ``EUDSL-BOUNDS``; the type-error text
is frontend-specific, so the tests pin the code plus the stable operand
substring only (ADR 0019 §1).  Legal offsets (including 0 and 24) and aligned
addresses keep the same HIR and static cost as before the change.
"""

from __future__ import annotations

import json
import unittest

from py import SetTo, Trigger, Var, build_module
from py.hir import emitVarStore
from py.tests.helpers import run_static
from py.view import EudslError

STATIC_KEYS = ("triggers", "conditions", "actions", "total_bytes")

TRIGGER_TARGET_STATIC = (4, 0, 67, 9_632)
ADDR_TARGET_STATIC = (3, 0, 67, 7_224)

ADDR_CONTROLS = (0x600000, 0x57F0B4, 0x6509B0)
LEGAL_OFFSETS = (0, 4, 8, 16, 20, 24)


def _static(hir_json: str) -> tuple[int, ...]:
    cost = run_static(hir_json)["static_cost"]
    return tuple(cost[key] for key in STATIC_KEYS)


def _varstore_node(module: object) -> dict[str, object]:
    stmts = json.loads(module.to_json())["module"]["stmts"]  # type: ignore[attr-defined]
    return next(stmt for stmt in stmts if stmt["kind"] == "varstore")


class VarStoreTargetTypeTests(unittest.TestCase):
    def _expect_error(
        self,
        target_for: object,
        code: str,
        substring: str,
    ) -> None:
        caught: list[EudslError] = []

        def build() -> None:
            t = Trigger(conds=[], acts=[])
            v = Var(3)
            with self.assertRaises(EudslError) as ctx:
                emitVarStore(v.id, target_for(t), SetTo)  # type: ignore[operator]
            caught.append(ctx.exception)

        build_module(build)

        self.assertEqual(len(caught), 1)
        self.assertEqual(caught[0].code, code)
        self.assertIn(substring, str(caught[0]))

    def test_float_addr_is_rejected_before_queueing(self) -> None:
        self._expect_error(
            lambda t: {"addr": 600000.5},
            "EUDSL-TYPE",
            "VarStore address must be a plain integer",
        )

    def test_numeric_string_addr_is_rejected_before_queueing(self) -> None:
        self._expect_error(
            lambda t: {"addr": "6291456"},
            "EUDSL-TYPE",
            "VarStore address must be a plain integer",
        )

    def test_float_trigger_id_is_rejected_before_queueing(self) -> None:
        self._expect_error(
            lambda t: {"trigger_id": t.id + 0.5, "offset": 0},
            "EUDSL-TYPE",
            "VarStore trigger_id must be a plain integer",
        )

    def test_float_offset_is_rejected_before_queueing(self) -> None:
        self._expect_error(
            lambda t: {"trigger_id": t.id, "offset": 20.7},
            "EUDSL-TYPE",
            "VarStore offset must be a plain integer",
        )

    def test_explicit_none_offset_is_rejected_not_defaulted(self) -> None:
        self._expect_error(
            lambda t: {"trigger_id": t.id, "offset": None},
            "EUDSL-TYPE",
            "VarStore offset must be a plain integer",
        )

    def test_empty_target_is_a_type_error_not_a_key_error(self) -> None:
        self._expect_error(
            lambda t: {},
            "EUDSL-TYPE",
            "VarStore trigger_id must be a plain integer",
        )


class VarStoreOffsetAlignmentTests(unittest.TestCase):
    def test_unaligned_offset_is_rejected_with_bounds(self) -> None:
        caught: list[EudslError] = []

        def build() -> None:
            t = Trigger(conds=[], acts=[])
            v = Var(3)
            with self.assertRaises(EudslError) as ctx:
                emitVarStore(v.id, {"trigger_id": t.id, "offset": 14}, SetTo)
            caught.append(ctx.exception)

        build_module(build)

        self.assertEqual(len(caught), 1)
        self.assertEqual(caught[0].code, "EUDSL-BOUNDS")
        self.assertEqual(
            str(caught[0]),
            "EUDSL-BOUNDS: VarStore offset 14 is not a multiple of 4",
        )


class VarStoreTargetControlTests(unittest.TestCase):
    def test_legal_offsets_are_stored_exactly(self) -> None:
        for offset in LEGAL_OFFSETS:
            with self.subTest(offset=offset):
                trigger_ids: list[int] = []

                def build(offset: int = offset) -> None:
                    t = Trigger(conds=[], acts=[])
                    trigger_ids.append(t.id)
                    v = Var(3)
                    emitVarStore(
                        v.id, {"trigger_id": t.id, "offset": offset}, SetTo
                    )

                module = build_module(build)
                node = _varstore_node(module)
                self.assertEqual(node["dst_trigger"], trigger_ids[0])
                self.assertEqual(node["dst_offset"], offset)
                self.assertEqual(_static(module.to_json()), TRIGGER_TARGET_STATIC)

    def test_omitted_offset_defaults_to_zero(self) -> None:
        trigger_ids: list[int] = []

        def build() -> None:
            t = Trigger(conds=[], acts=[])
            trigger_ids.append(t.id)
            v = Var(3)
            emitVarStore(v.id, {"trigger_id": t.id}, SetTo)

        module = build_module(build)
        node = _varstore_node(module)
        self.assertEqual(node["dst_trigger"], trigger_ids[0])
        self.assertEqual(node["dst_offset"], 0)
        self.assertEqual(_static(module.to_json()), TRIGGER_TARGET_STATIC)

    def test_aligned_addresses_keep_hir_and_static_cost(self) -> None:
        for addr in ADDR_CONTROLS:
            with self.subTest(addr=hex(addr)):
                def build(addr: int = addr) -> None:
                    v = Var(3)
                    emitVarStore(v.id, {"addr": addr}, SetTo)

                module = build_module(build)
                node = _varstore_node(module)
                self.assertEqual(node["dst_addr"], addr)
                self.assertEqual(_static(module.to_json()), ADDR_TARGET_STATIC)


if __name__ == "__main__":
    unittest.main()
