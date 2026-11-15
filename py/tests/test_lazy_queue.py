"""``lazyQueue`` / ``lazyDeque`` / ``lazyStack`` / ``lazyUnitGroup``
public API tests (globals P3).

The factories are thin public wrappers over the P1-01/P2-01 deferred
pattern on the eager container classes: a handle declared anywhere (no
builder needed) that resolves to a fully eager binding on first
resolving use inside ``build_module``, one binding per builder. Binding
creation is atomic (R3): one full eager constructor call -- every var,
out box, slot trigger, and auto-prune trigger in eager order. Eager
containers are unchanged; the lazy factories are opt-in only.

Compatible with both ``pytest`` and ``python3 -m unittest``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from py import (
    Deque,
    Queue,
    Stack,
    TriggerStruct,
    UnitGroup,
    Var,
    build_module,
    dword,
    lazyDeque,
    lazyDeque as _lazyDeque,
    lazyQueue,
    lazyStack,
    lazyUnitGroup,
    lazy_deque,
    lazy_queue,
    lazy_stack,
    lazy_unit_group,
    when,
)
from py.tests import lazy_queue_fixture as fixture
from py.view import EudslError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"

Point = TriggerStruct("Point", {"x": dword(), "y": dword()})

# Non-plain-int element operands (ADR 0019). `True` is an `int` subclass in
# Python, which is exactly the coercion the guard exists to stop. The labels
# are the TS sibling's, with the one-per-language "nothing" row called out:
# py has a single `None`, ts has `undefined` and `null`.
_BAD_ELEMENT_VALUES = (
    ("float", 1.5),
    ("nan", float("nan")),
    ("infinity", float("inf")),
    ("bool", True),
    ("str", "5"),
    ("none", None),
)


def run_dump_ir(hir_json: str) -> str:
    # Full pipeline comparison: `dump-ir` runs optimize_module then
    # lowering, so it exercises the storage analysis and the rendered
    # LIR. The comparison is the ENTIRE dump-ir output (storage report
    # including the per-var Allocations listing, plus the rendered LIR).
    with tempfile.TemporaryDirectory(prefix="eudkit-py-p3-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), "dump-ir", str(hir_path)],
            text=True,
        )
        assert "LIR v3.0" in out, "dump-ir output must contain an LIR section"
        assert (
            "Allocations:" in out
        ), "dump-ir output must contain the Allocations listing"
        return out


def hir_json(mod: Any) -> dict[str, Any]:
    return json.loads(mod.to_json())


def decls(mod: Any) -> list[list[int]]:
    return [[v["id"], v["init"]] for v in hir_json(mod)["module"]["vars"]]


def assert_code(test: unittest.TestCase, fn: Callable[[], Any], code: str) -> None:
    with test.assertRaises(EudslError) as cm:
        fn()
    test.assertEqual(cm.exception.code, code)


def error_of(fn: Callable[[], Any]) -> EudslError:
    try:
        fn()
    except EudslError as exc:
        return exc
    raise AssertionError("expected the call to throw")


def assert_dump_equal(
    test: unittest.TestCase,
    lazy_body: Callable[[], Any],
    eager_body: Callable[[], Any],
    label: str,
) -> None:
    lazy_mod = build_module(lazy_body)
    eager_mod = build_module(eager_body)
    test.assertEqual(
        run_dump_ir(lazy_mod.to_json()),
        run_dump_ir(eager_mod.to_json()),
        f"{label} must be dump-ir-identical to its eager twin",
    )


def out_of_order_case(
    test: unittest.TestCase,
    label: str,
    lazy_a: Any,
    lazy_b: Any,
    make_a: Callable[[], Any],
    make_b: Callable[[], Any],
    use: Callable[[Any], Any],
) -> None:
    lazy_ir = run_dump_ir(
        build_module(lambda: (use(lazy_b), use(lazy_a))).to_json()
    )
    b_first_ir = run_dump_ir(
        build_module(lambda: (use(make_b()), use(make_a()))).to_json()
    )

    def a_first() -> None:
        a = make_a()
        b = make_b()
        use(b)
        use(a)

    a_first_ir = run_dump_ir(build_module(a_first).to_json())
    test.assertEqual(lazy_ir, b_first_ir, f"{label}: B-first eager twin must match")
    test.assertNotEqual(
        lazy_ir, a_first_ir, f"{label}: declaration-order twin must differ"
    )


def conditional_first_use_case(
    test: unittest.TestCase,
    label: str,
    lazy: Any,
    make: Callable[[], Any],
    inside: Callable[[Any], Any],
    after: Callable[[Any], Any],
) -> None:
    def lazy_body() -> None:
        flag = Var(1)
        when(flag.eq(1), lambda: inside(lazy))
        after(lazy)

    eager: dict[str, Any] = {}

    def eager_body() -> None:
        flag = Var(1)

        def body() -> None:
            eager["handle"] = make()
            inside(eager["handle"])

        when(flag.eq(1), body)
        after(eager["handle"])

    test.assertEqual(
        run_dump_ir(build_module(lazy_body).to_json()),
        run_dump_ir(build_module(eager_body).to_json()),
        f"{label}: conditional first use must match the same-scope eager twin",
    )


def two_builder_case(
    test: unittest.TestCase,
    label: str,
    lazy: Any,
    make: Callable[[], Any],
    use: Callable[[Any], Any],
    id_of: Callable[[Any], int],
) -> None:
    ids: dict[str, int] = {}

    def body1() -> None:
        use(lazy)
        ids["first"] = id_of(lazy)

    def body2() -> None:
        first = Var(0)
        first.set(1)
        use(lazy)
        ids["second"] = id_of(lazy)

    def twin1() -> None:
        eager = make()
        use(eager)

    def twin2() -> None:
        first = Var(0)
        first.set(1)
        eager = make()
        use(eager)

    mod1 = build_module(body1)
    mod2 = build_module(body2)
    test.assertEqual(hir_json(mod1), hir_json(build_module(twin1)), label)
    test.assertEqual(hir_json(mod2), hir_json(build_module(twin2)), label)
    test.assertNotEqual(
        ids["first"], ids["second"], f"{label}: bindings must have different ids"
    )


def post_build_case(
    test: unittest.TestCase,
    label: str,
    make_lazy: Callable[[], Any],
    use: Callable[[Any], Any],
    id_of: Callable[[Any], int],
) -> None:
    single = make_lazy()
    captured: dict[str, int] = {}

    def single_body() -> None:
        use(single)
        captured["id"] = id_of(single)

    build_module(single_body)
    test.assertEqual(id_of(single), captured["id"], f"{label}: single record cached")

    multi = make_lazy()
    build_module(lambda: use(multi))
    build_module(lambda: use(multi))
    assert_code(test, lambda: id_of(multi), "EUDSL-SCOPE")

    never = make_lazy()
    assert_code(test, lambda: id_of(never), "EUDSL-SCOPE")


class LazyQueueTest(unittest.TestCase):
    def test_module_scope_declarations_allocate_nothing_and_pure_reads_work(self) -> None:
        q = lazyQueue("dword", 3)
        dq = lazyDeque("word", 2)
        st = lazyStack(4)
        ug = lazyUnitGroup(2, autoPrune=False, unlimiter=True)
        default_ug = lazyUnitGroup(2)

        self.assertEqual(q.capacity, 3)
        self.assertEqual(dq.capacity, 2)
        self.assertEqual(st.capacity, 4)
        self.assertEqual(ug.capacity, 2)
        self.assertFalse(ug.autoPrune)
        self.assertTrue(ug.unlimiter)
        self.assertTrue(default_ug.autoPrune)
        self.assertFalse(default_ug.unlimiter)

        def body() -> None:
            v = Var(0)
            v.set(1)

        with_handles = build_module(body)
        without_handles = build_module(body)
        # Identical output proves the unused handles consumed no ids,
        # emitted no decls/triggers, and no statements.
        self.assertEqual(hir_json(with_handles), hir_json(without_handles))
        self.assertEqual(decls(with_handles), [[0, 0]])

    def test_resolving_reads_outside_builder_throw(self) -> None:
        q = lazyQueue("dword", 2)
        assert_code(self, lambda: q.length, "EUDSL-SCOPE")
        assert_code(self, lambda: q.head, "EUDSL-SCOPE")
        assert_code(self, lambda: q.tail, "EUDSL-SCOPE")
        assert_code(self, lambda: q.outBox, "EUDSL-SCOPE")
        assert_code(self, lambda: q.out_box, "EUDSL-SCOPE")
        assert_code(self, lambda: q.slots, "EUDSL-SCOPE")
        assert_code(self, lambda: q.empty(), "EUDSL-SCOPE")
        assert_code(self, lambda: q.full(), "EUDSL-SCOPE")
        assert_code(self, lambda: q.append(1), "EUDSL-SCOPE")
        assert_code(self, lambda: q.popleft(), "EUDSL-SCOPE")
        assert_code(self, lambda: q.clear(), "EUDSL-SCOPE")

        dq = lazyDeque("dword", 2)
        assert_code(self, lambda: dq.length, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.left, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.right, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.outLeft, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.outRight, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.out_left, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.out_right, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.leftSlots, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.rightSlots, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.left_slots, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.right_slots, "EUDSL-SCOPE")
        assert_code(self, lambda: dq.append(1), "EUDSL-SCOPE")
        assert_code(self, lambda: dq.appendleft(1), "EUDSL-SCOPE")
        assert_code(self, lambda: dq.pop(), "EUDSL-SCOPE")
        assert_code(self, lambda: dq.popleft(), "EUDSL-SCOPE")

        st = lazyStack(2)
        assert_code(self, lambda: st.storage, "EUDSL-SCOPE")
        assert_code(self, lambda: st.pos, "EUDSL-SCOPE")
        assert_code(self, lambda: st.length, "EUDSL-SCOPE")
        assert_code(self, lambda: st.empty(), "EUDSL-SCOPE")
        assert_code(self, lambda: st.full(), "EUDSL-SCOPE")
        assert_code(self, lambda: st.push(1), "EUDSL-SCOPE")
        assert_code(self, lambda: st.pop(), "EUDSL-SCOPE")
        assert_code(self, lambda: st.clear(), "EUDSL-SCOPE")

        ug = lazyUnitGroup(2)
        assert_code(self, lambda: ug.length, "EUDSL-SCOPE")
        assert_code(self, lambda: ug.slots, "EUDSL-SCOPE")
        assert_code(self, lambda: ug.empty(), "EUDSL-SCOPE")
        assert_code(self, lambda: ug.full(), "EUDSL-SCOPE")
        assert_code(self, lambda: ug.add(1), "EUDSL-SCOPE")
        assert_code(self, lambda: ug.remove(1), "EUDSL-SCOPE")
        assert_code(self, lambda: ug.for_each(lambda _epd: None), "EUDSL-SCOPE")
        assert_code(self, lambda: ug.clear(), "EUDSL-SCOPE")

    def test_bounds_and_element_type_errors_match_eager_at_declaration(self) -> None:
        capacity_cases = [
            ("Queue", lambda: lazyQueue("dword", 0), lambda: Queue("dword", 0)),
            (
                "Queue fraction",
                lambda: lazyQueue("dword", 1.5),
                lambda: Queue("dword", 1.5),
            ),
            ("Deque", lambda: lazyDeque("word", -1), lambda: Deque("word", -1)),
            ("Stack", lambda: lazyStack(0), lambda: Stack(0)),
            ("UnitGroup", lambda: lazyUnitGroup(-1), lambda: UnitGroup(-1)),
        ]
        for label, lazy_call, eager_call in capacity_cases:
            lazy_error = error_of(lazy_call)
            eager_error = error_of(eager_call)
            self.assertEqual(lazy_error.code, "EUDSL-BOUNDS", label)
            self.assertEqual(str(lazy_error), str(eager_error), label)

        element_cases = [
            ("Queue", lambda: Queue("nope", 2), lambda: lazyQueue("nope", 2)),
            ("Deque", lambda: Deque("nope", 2), lambda: lazyDeque("nope", 2)),
        ]
        for label, eager_call, lazy_call in element_cases:
            # The eager constructor allocates vars before validating the
            # element, so capture its error inside a builder.
            eager_errors: list[EudslError] = []

            def eager_body() -> None:
                eager_errors.append(error_of(eager_call))

            build_module(eager_body)
            lazy_error = error_of(lazy_call)
            self.assertEqual(lazy_error.code, "EUDSL-BOUNDS", label)
            self.assertEqual(str(lazy_error), str(eager_errors[0]), label)

    def test_queue_core_operations_match_eager_twin(self) -> None:
        q = lazyQueue("dword", 3)

        def lazy_body() -> None:
            out = Var(0)
            q.append(1)
            q.push(2)
            out.set(q.popleft())
            out.set(q.pop())
            when(q.empty(), lambda: out.iadd(1))
            when(q.full(), lambda: out.iadd(2))
            out.set(q.length)
            out.set(q.head)
            out.set(q.tail)
            out.set(q.outBox)
            q.clear()

        def eager_body() -> None:
            out = Var(0)
            eager = Queue("dword", 3)
            eager.append(1)
            eager.push(2)
            out.set(eager.popleft())
            out.set(eager.pop())
            when(eager.empty(), lambda: out.iadd(1))
            when(eager.full(), lambda: out.iadd(2))
            out.set(eager.length)
            out.set(eager.head)
            out.set(eager.tail)
            out.set(eager.outBox)
            eager.clear()

        assert_dump_equal(self, lazy_body, eager_body, "Queue operations")

    def test_deque_core_operations_match_eager_twin(self) -> None:
        dq = lazyDeque("word", 3)

        def lazy_body() -> None:
            out = Var(0)
            dq.append(1)
            dq.appendleft(2)
            out.set(dq.pop())
            out.set(dq.popleft())
            when(dq.empty(), lambda: out.iadd(1))
            when(dq.full(), lambda: out.iadd(2))
            out.set(dq.length)
            out.set(dq.left)
            out.set(dq.right)
            out.set(dq.outLeft)
            out.set(dq.outRight)

        def eager_body() -> None:
            out = Var(0)
            eager = Deque("word", 3)
            eager.append(1)
            eager.appendleft(2)
            out.set(eager.pop())
            out.set(eager.popleft())
            when(eager.empty(), lambda: out.iadd(1))
            when(eager.full(), lambda: out.iadd(2))
            out.set(eager.length)
            out.set(eager.left)
            out.set(eager.right)
            out.set(eager.outLeft)
            out.set(eager.outRight)

        assert_dump_equal(self, lazy_body, eager_body, "Deque operations")

    def test_stack_core_operations_match_eager_twin(self) -> None:
        st = lazyStack(3)

        def lazy_body() -> None:
            out = Var(0)
            st.push(1)
            st.push(2)
            out.set(st.pop())
            when(st.empty(), lambda: out.iadd(1))
            when(st.full(), lambda: out.iadd(2))
            out.set(st.length)
            out.set(st.pos)
            out.set(st.storage.get(0))
            st.clear()

        def eager_body() -> None:
            out = Var(0)
            eager = Stack(3)
            eager.push(1)
            eager.push(2)
            out.set(eager.pop())
            when(eager.empty(), lambda: out.iadd(1))
            when(eager.full(), lambda: out.iadd(2))
            out.set(eager.length)
            out.set(eager.pos)
            out.set(eager.storage.get(0))
            eager.clear()

        assert_dump_equal(self, lazy_body, eager_body, "Stack operations")

    def test_unit_group_core_operations_and_auto_prune_match_eager_twin(self) -> None:
        ug = lazyUnitGroup(3)

        def lazy_body() -> None:
            out = Var(0)
            ug.add(1000)
            ug.add(2000)
            ug.remove(1000)
            ug.for_each(lambda epd: out.set(epd))
            when(ug.empty(), lambda: out.iadd(1))
            when(ug.full(), lambda: out.iadd(2))
            out.set(ug.length)
            out.set(ug.slots[0])
            ug.clear()

        def eager_body() -> None:
            out = Var(0)
            eager = UnitGroup(3)
            eager.add(1000)
            eager.add(2000)
            eager.remove(1000)
            eager.for_each(lambda epd: out.set(epd))
            when(eager.empty(), lambda: out.iadd(1))
            when(eager.full(), lambda: out.iadd(2))
            out.set(eager.length)
            out.set(eager.slots[0])
            eager.clear()

        assert_dump_equal(self, lazy_body, eager_body, "UnitGroup operations")

        no_prune = lazyUnitGroup(2, autoPrune=False)

        def lazy_no_prune() -> None:
            no_prune.add(7)
            no_prune.remove(7)

        def eager_no_prune() -> None:
            eager = UnitGroup(2, autoPrune=False)
            eager.add(7)
            eager.remove(7)

        assert_dump_equal(
            self, lazy_no_prune, eager_no_prune, "UnitGroup autoPrune=False"
        )

        unlimiter = lazyUnitGroup(2, unlimiter=True)

        def lazy_unlimiter() -> None:
            unlimiter.add(9)
            unlimiter.remove(9)

        def eager_unlimiter() -> None:
            eager = UnitGroup(2, unlimiter=True)
            eager.add(9)
            eager.remove(9)

        assert_dump_equal(
            self, lazy_unlimiter, eager_unlimiter, "UnitGroup unlimiter"
        )

    def test_struct_element_queue_and_deque_match_eager_twins(self) -> None:
        q = lazyQueue(Point, 2)

        def lazy_queue_body() -> None:
            out = Var(0)
            q.append(Point(x=7, y=9))
            q.append(Point(x=1, y=2))
            first = q.popleft()
            out.set(first.x)
            out.set(first.y)
            second = q.pop()
            out.set(second.x)

        def eager_queue_body() -> None:
            out = Var(0)
            # Arguments are evaluated before the resolving call, so the
            # first struct is allocated before the eager queue binding.
            first_arg = Point(x=7, y=9)
            eager = Queue(Point, 2)
            eager.append(first_arg)
            eager.append(Point(x=1, y=2))
            first = eager.popleft()
            out.set(first.x)
            out.set(first.y)
            second = eager.pop()
            out.set(second.x)

        assert_dump_equal(self, lazy_queue_body, eager_queue_body, "struct Queue")

        dq = lazyDeque(Point, 2)

        def lazy_deque_body() -> None:
            out = Var(0)
            dq.append(Point(x=3, y=4))
            dq.appendleft(Point(x=5, y=6))
            first = dq.popleft()
            out.set(first.x)
            second = dq.pop()
            out.set(second.y)

        def eager_deque_body() -> None:
            out = Var(0)
            # First-use order: the first argument is evaluated before the
            # binding exists, the second after it.
            first_arg = Point(x=3, y=4)
            eager = Deque(Point, 2)
            eager.append(first_arg)
            eager.appendleft(Point(x=5, y=6))
            first = eager.popleft()
            out.set(first.x)
            second = eager.pop()
            out.set(second.y)

        assert_dump_equal(self, lazy_deque_body, eager_deque_body, "struct Deque")

    def test_out_of_order_first_use_follows_first_use_order(self) -> None:
        out_of_order_case(
            self,
            "Queue",
            lazyQueue("dword", 2),
            lazyQueue("dword", 2),
            lambda: Queue("dword", 2),
            lambda: Queue("dword", 2),
            lambda handle: handle.append(1),
        )
        out_of_order_case(
            self,
            "Deque",
            lazyDeque("word", 2),
            lazyDeque("word", 2),
            lambda: Deque("word", 2),
            lambda: Deque("word", 2),
            lambda handle: handle.appendleft(1),
        )
        out_of_order_case(
            self,
            "Stack",
            lazyStack(2),
            lazyStack(2),
            lambda: Stack(2),
            lambda: Stack(2),
            lambda handle: handle.push(1),
        )
        out_of_order_case(
            self,
            "UnitGroup",
            lazyUnitGroup(2),
            lazyUnitGroup(2),
            lambda: UnitGroup(2),
            lambda: UnitGroup(2),
            lambda handle: handle.add(1000),
        )

    def test_conditional_first_use_matches_same_scope_eager_twin(self) -> None:
        conditional_first_use_case(
            self,
            "Queue",
            lazyQueue("dword", 2),
            lambda: Queue("dword", 2),
            lambda handle: handle.append(5),
            lambda handle: handle.popleft(),
        )
        conditional_first_use_case(
            self,
            "Deque",
            lazyDeque("word", 2),
            lambda: Deque("word", 2),
            lambda handle: handle.appendleft(5),
            lambda handle: handle.pop(),
        )
        conditional_first_use_case(
            self,
            "Stack",
            lazyStack(2),
            lambda: Stack(2),
            lambda handle: handle.push(5),
            lambda handle: handle.pop(),
        )
        conditional_first_use_case(
            self,
            "UnitGroup",
            lazyUnitGroup(2),
            lambda: UnitGroup(2),
            lambda handle: handle.add(1000),
            lambda handle: handle.remove(1000),
        )

    def test_never_used_handles_emit_nothing(self) -> None:
        q = lazyQueue("dword", 2)
        dq = lazyDeque("word", 2)
        st = lazyStack(2)
        ug = lazyUnitGroup(2)
        self.assertIsNotNone((q, dq, st, ug))

        def body() -> None:
            v = Var(0)
            v.set(1)

        with_handles = build_module(body)
        without_handles = build_module(body)
        self.assertEqual(hir_json(with_handles), hir_json(without_handles))
        self.assertEqual(decls(with_handles), [[0, 0]])
        self.assertIn("Total Variables: 1", run_dump_ir(with_handles.to_json()))

    def test_two_builder_use_creates_separate_bindings(self) -> None:
        two_builder_case(
            self,
            "Queue",
            lazyQueue("dword", 2),
            lambda: Queue("dword", 2),
            lambda handle: handle.append(1),
            lambda handle: handle.length.id,
        )
        two_builder_case(
            self,
            "Deque",
            lazyDeque("word", 2),
            lambda: Deque("word", 2),
            lambda handle: handle.appendleft(1),
            lambda handle: handle.length.id,
        )
        two_builder_case(
            self,
            "Stack",
            lazyStack(2),
            lambda: Stack(2),
            lambda handle: handle.push(1),
            lambda handle: handle.length.id,
        )
        two_builder_case(
            self,
            "UnitGroup",
            lazyUnitGroup(2),
            lambda: UnitGroup(2),
            lambda handle: handle.add(1000),
            lambda handle: handle.length.id,
        )

    def test_post_build_single_record_rule_and_no_post_build_allocation(self) -> None:
        post_build_case(
            self,
            "Queue",
            lambda: lazyQueue("dword", 2),
            lambda handle: handle.append(7),
            lambda handle: handle.length.id,
        )
        post_build_case(
            self,
            "Deque",
            lambda: lazyDeque("word", 2),
            lambda handle: handle.append(7),
            lambda handle: handle.length.id,
        )
        post_build_case(
            self,
            "Stack",
            lambda: lazyStack(2),
            lambda handle: handle.push(7),
            lambda handle: handle.length.id,
        )
        post_build_case(
            self,
            "UnitGroup",
            lambda: lazyUnitGroup(2),
            lambda handle: handle.add(7),
            lambda handle: handle.length.id,
        )

        # A post-build throw and a post-build cached read must both
        # allocate nothing: the next build still starts at id 0.
        def after_body() -> None:
            v = Var(0)
            v.set(1)

        self.assertEqual(decls(build_module(after_body)), [[0, 0]])

    def test_cross_file_handles_match_eager_twins(self) -> None:
        def lazy_body() -> None:
            out = Var(0)
            fixture.q.append(5)
            out.set(fixture.q.popleft())
            fixture.dq.append(7)
            fixture.dq.appendleft(6)
            out.set(fixture.dq.pop())
            fixture.st.push(9)
            out.set(fixture.st.pop())
            fixture.ug.add(1000)
            fixture.ug.remove(1000)

        def eager_body() -> None:
            out = Var(0)
            q = Queue("dword", 3)
            q.append(5)
            out.set(q.popleft())
            dq = Deque("word", 2)
            dq.append(7)
            dq.appendleft(6)
            out.set(dq.pop())
            st = Stack(2)
            st.push(9)
            out.set(st.pop())
            ug = UnitGroup(2, autoPrune=False)
            ug.add(1000)
            ug.remove(1000)

        assert_dump_equal(self, lazy_body, eager_body, "cross-file containers")

    def test_byte_identity_corpus_all_four_containers(self) -> None:
        cases: list[tuple[str, Callable[[], Any], Callable[[], Any]]] = []

        q = lazyQueue("dword", 4)

        def lazy_queue() -> None:
            out = Var(0)
            q.append(11)
            q.append(22)
            out.set(q.popleft())
            out.set(q.pop())
            q.clear()

        def eager_queue() -> None:
            out = Var(0)
            eager = Queue("dword", 4)
            eager.append(11)
            eager.append(22)
            out.set(eager.popleft())
            out.set(eager.pop())
            eager.clear()

        cases.append(("Queue dword", lazy_queue, eager_queue))

        q_byte = lazyQueue("byte", 3)

        def lazy_queue_byte() -> None:
            out = Var(0)
            q_byte.append(0x1FF)
            out.set(q_byte.popleft())

        def eager_queue_byte() -> None:
            out = Var(0)
            eager = Queue("byte", 3)
            eager.append(0x1FF)
            out.set(eager.popleft())

        cases.append(("Queue byte", lazy_queue_byte, eager_queue_byte))

        dq = lazyDeque("word", 4)

        def lazy_deque() -> None:
            out = Var(0)
            dq.append(1)
            dq.appendleft(2)
            out.set(dq.pop())
            out.set(dq.popleft())

        def eager_deque() -> None:
            out = Var(0)
            eager = Deque("word", 4)
            eager.append(1)
            eager.appendleft(2)
            out.set(eager.pop())
            out.set(eager.popleft())

        cases.append(("Deque word", lazy_deque, eager_deque))

        st = lazyStack(4)

        def lazy_stack() -> None:
            out = Var(0)
            st.push(1)
            st.push(2)
            out.set(st.pop())
            st.clear()

        def eager_stack() -> None:
            out = Var(0)
            eager = Stack(4)
            eager.push(1)
            eager.push(2)
            out.set(eager.pop())
            eager.clear()

        cases.append(("Stack", lazy_stack, eager_stack))

        ug = lazyUnitGroup(3)

        def lazy_unit_group() -> None:
            out = Var(0)
            ug.add(1000)
            ug.add(2000)
            ug.remove(1000)
            ug.for_each(lambda epd: out.set(epd))
            ug.clear()

        def eager_unit_group() -> None:
            out = Var(0)
            eager = UnitGroup(3)
            eager.add(1000)
            eager.add(2000)
            eager.remove(1000)
            eager.for_each(lambda epd: out.set(epd))
            eager.clear()

        cases.append(("UnitGroup auto-prune", lazy_unit_group, eager_unit_group))

        ug_no_prune = lazyUnitGroup(2, autoPrune=False, unlimiter=True)

        def lazy_unit_group_no_prune() -> None:
            ug_no_prune.add(1)
            ug_no_prune.remove(1)

        def eager_unit_group_no_prune() -> None:
            eager = UnitGroup(2, autoPrune=False, unlimiter=True)
            eager.add(1)
            eager.remove(1)

        cases.append(
            ("UnitGroup no-prune unlimiter", lazy_unit_group_no_prune, eager_unit_group_no_prune)
        )

        q_struct = lazyQueue(Point, 2)

        def lazy_queue_struct() -> None:
            out = Var(0)
            q_struct.append(Point(x=7, y=9))
            got = q_struct.popleft()
            out.set(got.x)

        def eager_queue_struct() -> None:
            out = Var(0)
            arg = Point(x=7, y=9)
            eager = Queue(Point, 2)
            eager.append(arg)
            got = eager.popleft()
            out.set(got.x)

        cases.append(("struct Queue", lazy_queue_struct, eager_queue_struct))

        dq_struct = lazyDeque(Point, 2)

        def lazy_deque_struct() -> None:
            out = Var(0)
            dq_struct.append(Point(x=1, y=2))
            dq_struct.appendleft(Point(x=3, y=4))
            got = dq_struct.pop()
            out.set(got.y)

        def eager_deque_struct() -> None:
            out = Var(0)
            first_arg = Point(x=1, y=2)
            eager = Deque(Point, 2)
            eager.append(first_arg)
            eager.appendleft(Point(x=3, y=4))
            got = eager.pop()
            out.set(got.y)

        cases.append(("struct Deque", lazy_deque_struct, eager_deque_struct))

        for name, lazy_body, eager_body in cases:
            lazy_mod = build_module(lazy_body)
            eager_mod = build_module(eager_body)
            self.assertEqual(
                run_dump_ir(lazy_mod.to_json()),
                run_dump_ir(eager_mod.to_json()),
                f"corpus case {name} must be byte-identical",
            )

    def _assert_type_error_in_builder(
        self, call: Callable[[], Any], *needles: str
    ) -> None:
        """Build `call` inside a fresh module and assert one `EUDSL-TYPE`.

        Catching inside the builder is the eagerness probe: the same module
        object is what the T-A3 byte-identity assertion compares.
        """
        captured: list[EudslError] = []

        def prog() -> None:
            try:
                call()
            except EudslError as exc:
                captured.append(exc)

        build_module(prog)
        self.assertEqual(len(captured), 1, "the call must raise inside the builder")
        self.assertEqual(captured[0].code, "EUDSL-TYPE")
        for needle in needles:
            self.assertIn(needle, str(captured[0]))

    def test_container_element_write_rejects_bad_operand(self) -> None:
        """A1: the element domain is a plain integer, a `Var`, or a struct.

        HEAD falls through as a `Var` and either dies in `pageSlotBitCond`
        with a raw `AttributeError` (py) or builds a silent value (ts).
        """
        writes: dict[str, Callable[[Any], Any]] = {
            "Queue.push": lambda value: Queue("dword", 4).push(value),
            "Queue.append": lambda value: Queue("dword", 4).append(value),
            "Deque.append": lambda value: Deque("dword", 4).append(value),
            "Deque.appendleft": lambda value: Deque("dword", 4).appendleft(value),
            "Stack.push": lambda value: Stack(4).push(value),
        }
        for name, write in writes.items():
            for label, value in _BAD_ELEMENT_VALUES:
                with self.subTest(write=name, operand=label):
                    self._assert_type_error_in_builder(
                        lambda: write(value),
                        "must be a plain integer",
                    )

    def test_container_element_write_controls_are_unchanged(self) -> None:
        """A2: the legal domain builds the same HIR as before the guard.

        The lengths are pinned literally and are identical on py and ts, so
        the two suites pin one number, not two.
        """
        controls: tuple[tuple[str, int, Callable[[], Any]], ...] = (
            ("Queue.push(0)", 6014, lambda: Queue("dword", 4).push(0)),
            ("Queue.push(-1)", 6050, lambda: Queue("dword", 4).push(-1)),
            ("Queue.push(0xFFFFFFFF)", 6050, lambda: Queue("dword", 4).push(0xFFFFFFFF)),
            ("Deque.append(0)", 9773, lambda: Deque("dword", 4).append(0)),
            ("Deque.append(-1)", 9845, lambda: Deque("dword", 4).append(-1)),
            (
                "Deque.append(0xFFFFFFFF)",
                9845,
                lambda: Deque("dword", 4).append(0xFFFFFFFF),
            ),
            ("Deque.appendleft(0)", 9773, lambda: Deque("dword", 4).appendleft(0)),
            ("Deque.appendleft(-1)", 9845, lambda: Deque("dword", 4).appendleft(-1)),
            (
                "Deque.appendleft(0xFFFFFFFF)",
                9845,
                lambda: Deque("dword", 4).appendleft(0xFFFFFFFF),
            ),
            ("Stack.push(0)", 3190, lambda: Stack(4).push(0)),
            ("Stack.push(-1)", 3199, lambda: Stack(4).push(-1)),
            ("Stack.push(0xFFFFFFFF)", 3199, lambda: Stack(4).push(0xFFFFFFFF)),
            ("Queue.push(Var(0))", 6732, lambda: Queue("dword", 4).push(Var(0))),
            (
                "Queue(Point,4).push(Point)",
                10247,
                lambda: Queue(Point, 4).push(Point(x=1, y=2)),
            ),
        )
        for label, want, body in controls:
            with self.subTest(control=label):
                self.assertEqual(len(build_module(body).to_json()), want, label)

    def test_container_element_error_emits_nothing(self) -> None:
        """A3: the guard fires before the first trigger of the write.

        HEAD leaks the full-advance `when` plus constant acts (py 3418) or
        the whole slot write (ts 6014) before failing; after the fix the
        failed module is byte-equal to the container-only control.
        """
        def build(fail: Callable[[], Any]) -> str:
            captured: list[EudslError] = []

            def prog() -> None:
                try:
                    fail()
                except EudslError as exc:
                    captured.append(exc)

            mod = build_module(prog)
            self.assertEqual(len(captured), 1, "the call must raise inside the builder")
            self.assertEqual(captured[0].code, "EUDSL-TYPE")
            return mod.to_json()

        def control(body: Callable[[], Any]) -> str:
            return build_module(body).to_json()

        queue_failed = build(lambda: Queue("dword", 4).push(1.5))
        queue_control = control(lambda: Queue("dword", 4))
        # 1984 is the TS sibling's control length too (same fixture).
        self.assertEqual(len(queue_control), 1984)
        self.assertEqual(queue_failed, queue_control)

        stack_failed = build(lambda: Stack(4).push(1.5))
        stack_control = control(lambda: Stack(4))
        self.assertEqual(len(stack_control), 206)
        self.assertEqual(stack_failed, stack_control)

    def test_container_element_shape_must_match_the_element(self) -> None:
        """A4: the element must supply one value per slot field.

        HEAD raises a raw `IndexError` (a `Var` into a 2-field element) or
        silently drops the extra fields (a 3-field struct builds 177174, a
        2-field struct into a 1-field element builds 90688).
        """
        Point3 = TriggerStruct("Point3", {"x": dword(), "y": dword(), "z": dword()})
        rows: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("Queue(Point,3).push(Var(0))", lambda: Queue(Point, 3).push(Var(0))),
            (
                "Queue(Point,3).push(Point3)",
                lambda: Queue(Point, 3).push(Point3(x=1, y=2, z=3)),
            ),
            (
                'Queue("dword",4).push(Point)',
                lambda: Queue("dword", 4).push(Point(x=1, y=2)),
            ),
        )
        for label, call in rows:
            with self.subTest(row=label):
                self._assert_type_error_in_builder(
                    call, "shape must match the element"
                )

    def test_lazy_container_element_write_inherits_the_guard(self) -> None:
        """A5: the guard sits below the lazy seam (lazy resolves to eager)."""
        q = lazyQueue("dword", 3)
        self._assert_type_error_in_builder(
            lambda: q.push(1.5),
            "container element must be a plain integer",
        )

    def test_snake_case_aliases_are_same_factories(self) -> None:
        self.assertIs(lazy_queue, lazyQueue)
        self.assertIs(lazy_deque, lazyDeque)
        self.assertIs(lazy_stack, lazyStack)
        self.assertIs(lazy_unit_group, lazyUnitGroup)
        self.assertIs(_lazyDeque, lazyDeque)


if __name__ == "__main__":
    unittest.main()
