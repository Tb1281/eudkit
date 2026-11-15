"""Issue #86 (C3): size-gated ``FixedArray.get`` via the PageSlotEPD reader.

From ``length >= DYNAMIC_GET_MIN_LENGTH`` a dynamic ``get`` reads the element
through the ``CurrentPlayer`` register instead of unrolling one
``when(index.eq(k))`` branch per element: seed CP at ``EPD(elem0.value)``, add
one carry-free PageSlotEPD weight per index bit, reset ``out``, run 32 guarded
bit tests, then reset CP to the canonical loop-start ``0`` (ADR 0004; the
spike's ``zero`` policy).

Measured on this branch (reader-only module, debug simulator):

| N   | unroll static/exec | reader static/exec | reader bytes |
|-----|--------------------|--------------------|--------------|
| 63  | 190 / 66           | — (below gate)     | —            |
| 64  | 193 / 67           | 42 / 41            | 101,144      |
| 65  | 197 / 68           | 44 / 42            | 105,960      |
| 128 | 386 / 131          | 44 / 42            | 105,960      |
| 256 | 772 / 259          | 47 / 43            | 113,184      |

The 64 threshold keeps the cost-gate fixture (N=4) on the unroll, so
``cost_gate.expected.json`` stays byte-identical. Spike #80 provenance:
``docs/spikes/c3-dynamic-get/README.md``.

CP contract: the reader treats ``CurrentPlayer`` as scratch and leaves it at
``0``. A non-zero ambient CP is not preserved; a ``with_cp`` cursor that moved
re-anchors on exit, one that did not is silently landed on ``0``.
"""

from __future__ import annotations

import unittest

from py import (
    DYNAMIC_GET_MIN_LENGTH,
    FixedArray,
    FixedArrayImpl,
    Var,
    arrayDword,
    build_module,
    when,
    with_cp,
)
from py.tests.helpers import run_sim, run_static
from py.view import EPD_CURRENT_PLAYER

#: Reader-only cost pins: (N, static T/C/A, trigger bytes, total bytes, exec T/C/A).
READER_COST_CASES = [
    (64, (42, 70, 105), 101_136, 101_144, (41, 70, 4)),
    (65, (44, 71, 170), 105_952, 105_960, (42, 71, 4)),
    (128, (44, 71, 170), 105_952, 105_960, (42, 71, 4)),
    (256, (47, 72, 299), 113_176, 113_184, (43, 72, 4)),
]


def _reader_only(length: int):
    def prog() -> None:
        arr = FixedArray(arrayDword, length)
        arr.get(Var(1))

    return prog


def _unroll_get(arr: FixedArrayImpl, index: Var) -> Var:
    """The pre-#86 read path, used as the parity reference above the gate."""
    out = Var(0)
    for k in range(arr.length):
        elem = arr.vars[k]
        when(index.eq(k), lambda elem=elem: out.set(elem))
    return out


class DynamicGetGateTests(unittest.TestCase):
    def test_threshold_is_pinned(self):
        # Mirrored in ts/array.ts as DYNAMIC_GET_MIN_LENGTH; there is no
        # shared constants module, so both suites pin the literal.
        self.assertEqual(DYNAMIC_GET_MIN_LENGTH, 64)

    def test_n63_stays_on_the_unroll_and_n64_switches_to_the_reader(self):
        static = run_static(build_module(_reader_only(63)).to_json())["static_cost"]
        self.assertEqual(
            (static["triggers"], static["conditions"], static["actions"]),
            (190, 63, 379),
            "N=63 must stay on the 3N+pages unroll (3*63+1)",
        )

        static = run_static(build_module(_reader_only(64)).to_json())["static_cost"]
        self.assertEqual(
            (static["triggers"], static["conditions"], static["actions"]),
            (42, 70, 105),
            "N=64 is the first reader size (35+bits+pages)",
        )


class DynamicGetCostTests(unittest.TestCase):
    def test_each_size_pins_its_own_static_and_executed_cost(self):
        for length, static_pin, trigger_bytes, total_bytes, exec_pin in READER_COST_CASES:
            with self.subTest(length=length):
                js = build_module(_reader_only(length)).to_json()
                static = run_static(js)["static_cost"]
                self.assertEqual(
                    (static["triggers"], static["conditions"], static["actions"]),
                    static_pin,
                )
                self.assertEqual(static["trigger_bytes"], trigger_bytes)
                self.assertEqual(static["total_bytes"], total_bytes)
                executed = run_sim(js, loops=1)["executed_cost"]
                self.assertEqual(
                    (
                        executed["triggers"],
                        executed["conditions"],
                        executed["actions"],
                    ),
                    exec_pin,
                )

    def test_n256_reproduces_the_spike_headline(self):
        js = build_module(_reader_only(256)).to_json()
        static = run_static(js)["static_cost"]
        executed = run_sim(js, loops=1)["executed_cost"]
        self.assertEqual(static["triggers"], 47)
        self.assertEqual(static["total_bytes"], 113_184)
        self.assertEqual(executed["triggers"], 43)


class DynamicGetParityTests(unittest.TestCase):
    def _reader_vs_unroll(self, length: int, indices: tuple[int, ...]):
        ids: dict[str, int] = {}

        def prog() -> None:
            arr = FixedArray(arrayDword, length)
            for i in range(length):
                arr.set(i, 1000 + i)
            for idx in indices:
                ids[f"reader{idx}"] = arr.get(Var(idx)).id
                ids[f"unroll{idx}"] = _unroll_get(arr, Var(idx)).id

        report = run_sim(build_module(prog).to_json(), loops=1)
        return report["var_values"], ids

    def test_reader_matches_the_unroll_including_cross_page_indices(self):
        cases = {
            64: (0, 63),
            65: (0, 63, 64),
            128: (0, 63, 64, 127),
            256: (0, 63, 64, 127, 128, 191, 192, 255),
        }
        for length, indices in cases.items():
            with self.subTest(length=length):
                values, ids = self._reader_vs_unroll(length, indices)
                for idx in indices:
                    want = 1000 + idx
                    self.assertEqual(
                        values[ids[f"reader{idx}"]], want, f"reader idx {idx}"
                    )
                    self.assertEqual(
                        values[ids[f"unroll{idx}"]], want, f"unroll idx {idx}"
                    )

    def test_out_of_range_index_matches_the_unroll_zero(self):
        for length, indices in {
            64: (64, 300),
            65: (65, 300),
            256: (256, 300, 1024),
        }.items():
            with self.subTest(length=length):
                values, ids = self._reader_vs_unroll(length, indices)
                for idx in indices:
                    self.assertEqual(
                        values[ids[f"reader{idx}"]], 0, f"reader idx {idx}"
                    )
                    self.assertEqual(
                        values[ids[f"unroll{idx}"]], 0, f"unroll idx {idx}"
                    )

    def test_two_reads_at_one_site_reseed_cp(self):
        ids: dict[str, int] = {}

        def prog() -> None:
            arr = FixedArray(arrayDword, 64)
            for i in range(64):
                arr.set(i, 2000 + i)
            idx = Var(3)
            ids["a"] = arr.get(idx).id
            ids["b"] = arr.get(Var(11)).id

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(
            [report["var_values"][ids["a"]], report["var_values"][ids["b"]]],
            [2003, 2011],
        )

    def test_replayed_read_seeds_afresh_on_every_pass(self):
        """A read inside a loop body must not inherit the previous PCB pointer."""
        ids: dict[str, int] = {}

        def prog() -> None:
            arr = FixedArray(arrayDword, 64)
            for i in range(64):
                arr.set(i, 3000 + i)
            phase = Var(0)
            out = Var(0)
            ids["out"] = out.id
            for p, idx in enumerate((5, 63, 64, 0)):
                def body(idx=idx) -> None:
                    out.set(arr.get(Var(idx)))

                when(phase.eq(p), body)
            phase.iadd(1)

        report = run_sim(build_module(prog).to_json(), loops=4)
        # 5 -> 3005, 63 -> 3063, 64 is out of range -> 0, 0 -> 3000.
        self.assertEqual(report["var_values"][ids["out"]], 3000)


class DynamicGetCpTests(unittest.TestCase):
    def test_reader_outside_with_cp_leaves_cp_at_zero(self):
        ids: dict[str, int] = {}

        def prog() -> None:
            arr = FixedArray(arrayDword, 64)
            for i in range(64):
                arr.set(i, 3000 + i)
            ids["out"] = arr.get(Var(5)).id

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(report["var_values"][ids["out"]], 3005)
        self.assertEqual(report["external_epds"].get(str(EPD_CURRENT_PLAYER)), 0)

    def test_reader_inside_an_unmoved_with_cp_lands_cp_on_zero(self):
        """Documented hazard: the `zero` policy drops the ambient value.

        `CPCursor.restore()` only re-anchors when the build-time offset is
        non-zero, so a body that reads without hopping never restores the
        ambient CP. This pins today's contract, not a desired one.
        """
        ids: dict[str, int] = {}

        def prog() -> None:
            arr = FixedArray(arrayDword, 64)
            for i in range(64):
                arr.set(i, 3000 + i)

            def body(cp) -> None:
                ids["out"] = arr.get(Var(5)).id

            with_cp(7000, body)

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(report["var_values"][ids["out"]], 3005)
        self.assertEqual(report["external_epds"].get(str(EPD_CURRENT_PLAYER)), 0)

    def test_reader_inside_a_moved_with_cp_restores_the_base(self):
        ids: dict[str, int] = {}

        def prog() -> None:
            arr = FixedArray(arrayDword, 64)
            for i in range(64):
                arr.set(i, 3000 + i)

            def body(cp) -> None:
                cp.moveTo(3)
                ids["out"] = arr.get(Var(5)).id

            with_cp(7000, body)

        report = run_sim(build_module(prog).to_json(), loops=1)
        self.assertEqual(report["var_values"][ids["out"]], 3005)
        self.assertEqual(report["external_epds"].get(str(EPD_CURRENT_PLAYER)), 7000)


class DynamicGetEmissionTests(unittest.TestCase):
    def test_reader_emits_the_guarded_bit_tests(self):
        """Guard is mandatory: +32 conditions, +0 triggers, +0 bytes.

        The unguarded reader would read whatever `EPD(elem0) + weight(index)`
        points at for `index >= length`; the unroll returns 0. The bit tests
        therefore carry ``bits`` weight conditions plus 32 bit conditions plus
        32 guard conditions.
        """
        js = build_module(_reader_only(256)).to_json()
        static = run_static(js)["static_cost"]
        self.assertEqual(static["conditions"], 8 + 32 + 32)

    def test_no_extra_trigger_for_the_guard(self):
        # One trigger each for the seed, the reset and the CP zero, `bits`
        # weight triggers, 32 bit tests, plus one backing page per 64 slots.
        static64 = run_static(build_module(_reader_only(64)).to_json())["static_cost"]
        static256 = run_static(build_module(_reader_only(256)).to_json())["static_cost"]
        self.assertEqual(static64["triggers"], 1 + 1 + 6 + 1 + 32 + 1)
        self.assertEqual(static256["triggers"], 4 + 1 + 8 + 1 + 32 + 1)


if __name__ == "__main__":
    unittest.main()
