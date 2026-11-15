"""ADR 0027: >16 condition auto-chunking for structured control flow.

Python mirror of ``ts/tests/test_cond_chunking.ts``.

A raw ``Trigger (16C, 64A)`` stays capped at 16 conditions
(``EUDSL-BOUNDS``). The structured forms below must split their condition
list into 16-condition chunks so 17/32/33-condition gates lower and assemble
cleanly:

  when (with/without else, empty then + else), once, until,
  whileLoop, onRising, onFalling.

The ``<=16`` shape is pinned in ``crates/eudkit-lower/src/control.rs``; these
tests cover lowering, assembly, and runtime behaviour for the chunked path.

``once`` with an empty body cannot be exercised through the frontend: opt
drops it before lowering. That direct-lowering case is pinned in
``crates/eudkit-lower/tests/cond_chunking.rs``.
"""

import unittest

from py import (
  Var,
  build_module,
  on_falling,
  on_rising,
  once,
  until,
  when,
  while_loop,
)

from py.tests.helpers import run_sim, run_static

FORMS = [
  ("when", 1),
  ("when-else", 1),
  ("when-empty-else", 0),
  ("once", 1),
  ("until", 0),
  ("while", 1),
  ("onRising", 1),
  ("onFalling", 1),
]


def make_module(form: str, n: int, init: int):
  """Build one structured form with ``n`` distinct condition Vars.

  ``marker`` is always ``var_values[0]``; every condition is ``v.eq(1)`` and
  ``init`` chooses whether the gate is true or false on loop 1.
  """

  def make() -> None:
    marker = Var(0)
    condition_vars = [Var(init) for _ in range(n)]
    conds = [v.eq(1) for v in condition_vars]

    if form == "when":
      when(conds, lambda: marker.iadd(1))
    elif form == "when-else":
      # Distinguish the arms so a false gate cannot pass by running then.
      when(conds, lambda: marker.iadd(10), lambda: marker.iadd(1))
    elif form == "when-empty-else":
      when(conds, lambda: None, lambda: marker.iadd(1))
    elif form == "once":
      once(conds, lambda: marker.iadd(1))
    elif form == "until":
      until(conds, lambda: marker.iadd(1))
    elif form == "while":

      def body() -> None:
        # Break the loop after the first body pass so `sim` terminates.
        condition_vars[0].set(0)
        marker.iadd(1)

      while_loop(conds, body)
    elif form == "onRising":
      on_rising(conds, lambda: marker.iadd(1))
    elif form == "onFalling":
      frame = Var(0)
      frame.iadd(1)

      def clear() -> None:
        for v in condition_vars:
          v.set(0)

      when(frame.eq(2), clear)
      on_falling(conds, lambda: marker.iadd(1))
    else:  # pragma: no cover - guard against a typo in FORMS
      raise AssertionError(f"unknown form {form}")

  return build_module(make)


def report_for(form: str, n: int, init: int) -> dict:
  return run_static(make_module(form, n, init).to_json())


def make_sequence_module(form: str, n: int):
  """Frame-ordered module: frame 1 T/F, frame 2 F/T, frames 3-4 T/T.

  The setup ``when`` statements run before the chunked gate in the same
  trigger loop.
  """

  def make() -> None:
    marker = Var(0)
    frame = Var(0)
    frame.iadd(1)
    condition_vars = [Var(1) for _ in range(n)]
    conds = [v.eq(1) for v in condition_vars]

    when(frame.eq(1), lambda: condition_vars[n - 1].set(0))

    def frame2() -> None:
      condition_vars[0].set(0)
      condition_vars[n - 1].set(1)

    when(frame.eq(2), frame2)
    when(frame.eq(3), lambda: condition_vars[0].set(1))

    if form == "when":
      when(conds, lambda: marker.iadd(1))
    else:
      once(conds, lambda: marker.iadd(1))

  return build_module(make)


class CondChunkingTests(unittest.TestCase):
  def assert_all_triggers_chunked(
    self, report: dict, label: str, n: int
  ) -> None:
    self.assertGreaterEqual(
      report["static_cost"]["conditions"],
      n,
      f"{label} n={n}: all {n} conditions must survive opt",
    )
    for entry in report["packing"]:
      self.assertLessEqual(
        entry["conditions"],
        16,
        f"{label} n={n}: trigger[{entry['item']}] carries "
        f"{entry['conditions']} conditions; ADR 0027 requires <= 16",
      )
    condition_bearing = [
      entry for entry in report["packing"] if entry["conditions"] > 0
    ]
    self.assertGreaterEqual(
      len(condition_bearing),
      (n + 15) // 16,
      f"{label} n={n}: expected at least {(n + 15) // 16} "
      f"condition-bearing triggers, saw {len(condition_bearing)}",
    )

  def test_all_structured_forms_lower_and_assemble_at_17_32_33(self) -> None:
    for n in (17, 32, 33):
      for form, init in FORMS:
        with self.subTest(form=form, n=n):
          self.assert_all_triggers_chunked(report_for(form, n, init), form, n)

  def test_16_or_fewer_conditions_keep_the_single_gate_shape(self) -> None:
    # 16 conditions must remain one 16C trigger plus body/restore, not get
    # pre-chunked. Any chunking rewrite that splits at <=16 shows up here.
    for n in (0, 1, 16):
      with self.subTest(n=n):
        report = report_for("when", n, 1)
        condition_bearing = [
          entry for entry in report["packing"] if entry["conditions"] > 0
        ]
        self.assertEqual(
          len(condition_bearing),
          0 if n == 0 else 1,
          f"n={n}: <=16 when must keep a single condition-bearing gate",
        )
        for entry in condition_bearing:
          self.assertEqual(
            entry["conditions"],
            n,
            f"n={n}: the gate must carry exactly {n} conditions",
          )

  def test_chunked_gates_keep_runtime_semantics_at_17(self) -> None:
    cases = [
      ("when true", "when", 1, 2, 2),
      ("when false", "when", 0, 2, 0),
      ("when true/else", "when-else", 1, 2, 20),
      ("when false/else", "when-else", 0, 2, 2),
      ("when empty-then/else", "when-empty-else", 0, 2, 2),
      ("once", "once", 1, 3, 1),
      ("until (conds false)", "until", 0, 2, 2),
      ("whileLoop", "while", 1, 2, 1),
      ("onRising", "onRising", 1, 2, 1),
    ]
    for label, form, init, loops, expected in cases:
      with self.subTest(label=label):
        report = run_sim(make_module(form, 17, init).to_json(), loops)
        self.assertEqual(
          report["var_values"][0],
          expected,
          f"{label}: marker after {loops} loop(s)",
        )

  def test_all_false_chunked_gate_costs_exactly_1t(self) -> None:
    for n in (17, 33):
      with self.subTest(n=n):
        report = run_sim(make_module("when", n, 0).to_json(), 1)
        self.assertEqual(
          report["executed_cost"]["triggers"],
          1,
          f"n={n}: an all-false chunked gate must cost exactly 1T",
        )
        self.assertEqual(
          report["executed_cost"]["visited_items"],
          [0],
          f"n={n}: only g0 may be evaluated on an all-false pass",
        )

  def test_chunked_when_and_once_keep_tf_ft_tt_frame_order(self) -> None:
    for form in ("when", "once"):
      with self.subTest(form=form):
        mod = make_sequence_module(form, 17)
        markers = [run_sim(mod.to_json(), loops)["var_values"][0] for loops in (1, 2, 3, 4)]
        expected = [0, 0, 1, 2] if form == "when" else [0, 0, 1, 1]
        self.assertEqual(
          markers,
          expected,
          f"{form}: marker after T/F, F/T, T/T, T/T frames",
        )

  def test_chunked_while_executes_multiple_body_passes_before_chunk_1_fails(
    self,
  ) -> None:
    n = 33

    def make() -> None:
      marker = Var(0)
      condition_vars = [Var(2 if i == 16 else 1) for i in range(n)]
      conds = [
        v.ge(1) if i == 16 else v.eq(1) for i, v in enumerate(condition_vars)
      ]

      def body() -> None:
        marker.iadd(1)
        condition_vars[16].isub(1)

      while_loop(conds, body)

    report = run_sim(build_module(make).to_json(), 1)
    self.assertEqual(
      report["var_values"][0],
      2,
      "the while body must execute twice before chunk 1 fails",
    )
    self.assertEqual(
      report["var_values"][17],
      0,
      "the chunk-1 countdown must reach zero",
    )

  def test_on_falling_fires_only_on_all_false_loop_after_17_true(self) -> None:
    mod = make_module("onFalling", 17, 1)
    self.assertEqual(
      run_sim(mod.to_json(), 1)["var_values"][0],
      0,
      "a true loop is not a falling edge",
    )
    self.assertEqual(
      run_sim(mod.to_json(), 2)["var_values"][0],
      1,
      "the first all-false loop must fire exactly once",
    )


if __name__ == "__main__":
  unittest.main()
