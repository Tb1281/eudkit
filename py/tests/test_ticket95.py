"""Ticket #95/#99: canonical EUDSL-WARN-SCOPE contract for lazy* declarations.

The message is one exact string for every lazy* factory in both frontends,
matching ADR 0005 and GLOSSARY.md.
"""

from __future__ import annotations

import unittest
import warnings

from py import (
    arrayByte,
    build_module,
    defineStruct,
    lazyDeque,
    lazyFixedArray,
    lazyFixedString,
    lazyQueue,
    lazyStack,
    lazyStruct,
    lazyUnitGroup,
    lazyVar,
    lazy_deque,
    lazy_fixed_array,
    lazy_fixed_string,
    lazy_queue,
    lazy_stack,
    lazy_struct,
    lazy_unit_group,
    lazy_var,
    on_warning,
)


def capture_warnings(action):
    captured = []
    unsubscribe = on_warning(lambda code, msg: captured.append((code, msg)))
    try:
        action()
    finally:
        unsubscribe()
    return captured


Point = defineStruct("Point", {"x": 4, "y": 4})

SCOPE_CODE = "EUDSL-WARN-SCOPE"
SCOPE_MESSAGE = (
    "EUDSL-WARN-SCOPE: lazy* declared inside buildModule; "
    "use eager Var for in-module declarations"
)

FACTORIES = [
    ("lazyVar", lambda: lazyVar(0)),
    ("lazyFixedString", lambda: lazyFixedString(32)),
    ("lazyFixedArray", lambda: lazyFixedArray(arrayByte, 8)),
    ("lazyQueue", lambda: lazyQueue(arrayByte, 8)),
    ("lazyDeque", lambda: lazyDeque(arrayByte, 8)),
    ("lazyStack", lambda: lazyStack(8)),
    ("lazyUnitGroup", lambda: lazyUnitGroup(8)),
    ("lazyStruct", lambda: lazyStruct(Point)),
]

ALIAS_FACTORIES = [
    ("lazyVar", lambda: lazy_var(0)),
    ("lazyFixedString", lambda: lazy_fixed_string(32)),
    ("lazyFixedArray", lambda: lazy_fixed_array(arrayByte, 8)),
    ("lazyQueue", lambda: lazy_queue(arrayByte, 8)),
    ("lazyDeque", lambda: lazy_deque(arrayByte, 8)),
    ("lazyStack", lambda: lazy_stack(8)),
    ("lazyUnitGroup", lambda: lazy_unit_group(8)),
    ("lazyStruct", lambda: lazy_struct(Point)),
]


class Ticket95ScopeWarningTest(unittest.TestCase):
    def test_lazy_constructors_emit_scope_warning_inside_build_module(self):
        for name, invoke in FACTORIES:
            with self.subTest(name=name):
                captured = capture_warnings(
                    lambda invoke=invoke: build_module(lambda: invoke())
                )
                self.assertEqual(
                    len(captured),
                    1,
                    f"Expected exactly 1 warning when {name} is called inside build_module, got {len(captured)}",
                )
                code, msg = captured[0]
                self.assertEqual(code, SCOPE_CODE)
                self.assertEqual(msg, SCOPE_MESSAGE)

    def test_lazy_aliases_emit_scope_warning_inside_build_module(self):
        for name, invoke in ALIAS_FACTORIES:
            with self.subTest(name=name):
                captured = capture_warnings(
                    lambda invoke=invoke: build_module(lambda: invoke())
                )
                self.assertEqual(
                    len(captured),
                    1,
                    f"Expected exactly 1 warning when alias for {name} is called inside build_module, got {len(captured)}",
                )
                code, msg = captured[0]
                self.assertEqual(code, SCOPE_CODE)
                self.assertEqual(msg, SCOPE_MESSAGE)

    def test_lazy_constructors_emit_no_warning_outside_build_module(self):
        for name, invoke in FACTORIES:
            with self.subTest(name=name):
                captured = capture_warnings(invoke)
                self.assertEqual(
                    len(captured),
                    0,
                    f"Expected 0 warnings when {name} is called outside build_module, got {len(captured)}",
                )

    def test_resolving_external_handles_inside_build_module_emits_no_warning(self):
        var = lazyVar(0)
        array = lazyFixedArray(arrayByte, 4)

        captured = capture_warnings(
            lambda: build_module(lambda: (var.set(1), array.get(0).set(1)))
        )

        self.assertEqual(
            captured,
            [],
            f"Expected 0 warnings when resolving externally declared lazy handles, got {captured}",
        )
