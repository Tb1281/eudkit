"""C11-02 — Until desugar: DSL node emission + byte-identity tests.

Python mirror of ts/test_until_desugar.ts. The new pipeline (DSL emits
`HirUntil`, opt desugars it) must produce compiled output byte-identical
to the old pipeline (DSL desugars to 2 whens) for every case below,
including variables allocated after the `until` call. That holds
because the DSL reserves the latch variable at node-emission time, in
exactly the old allocation order, and carries it on the node as
`latch_var`; the opt desugar uses that id verbatim and never mints ids.
(The design doc's Q6 mechanism — minting in opt via `next_id` +
appended declarations — was corrected here: `next_id` is the
trigger-id namespace, and appended-late ids sort last in
`analyze_storage`'s id-order ranking, shifting ranks.) Old-shape
programs are written with the surviving `when` + `Var` primitives, so
they pin the old output without depending on the deleted DSL desugar.

P5/P6 are the discriminating cases for the correction: a bit-class
variable allocated after `until` must NOT swap bit positions with the
latch. Those tests assert the new output equals the direct old output
exactly (and differs from the swapped-layout old output).
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from py import Var, build_module, once, until, when, while_loop

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"


def run_dump_ir(hir_json: str) -> str:
  # Full pipeline comparison: `dump-ir` runs optimize_module then
  # lowering, so it exercises the opt desugar. The comparison is the
  # ENTIRE dump-ir output (storage report including the per-var
  # Allocations listing, plus the rendered LIR): with the latch reserved
  # at the call site, the new program's declarations, ids, and
  # allocation mapping are identical to the old program's, so nothing
  # needs to be dropped.
  with tempfile.TemporaryDirectory(prefix="eudkit-py-until-") as tmpdir:
    hir_path = Path(tmpdir) / "prog.hir.json"
    hir_path.write_text(hir_json, encoding="utf-8")
    out = subprocess.check_output(
      [str(EUDKIT_COMPILE), "dump-ir", str(hir_path)],
      text=True,
    )
  assert "LIR v3.0" in out, "dump-ir output must contain an LIR section"
  return out


def new_p1():
  def make():
    frame = Var(0)
    count = Var(0)
    frame.iadd(1)

    def body():
      count.iadd(1)

    until(frame.eq(2), body)

  return build_module(make)


def old_p1():
  def make():
    frame = Var(0)
    count = Var(0)
    frame.iadd(1)
    active = Var(1)

    def latch_off():
      active.set(0)

    def body():
      count.iadd(1)

    when(active.eq(1), lambda: when(frame.eq(2), latch_off))
    when(active.eq(1), body)

  return build_module(make)


class UntilDesugarTests(unittest.TestCase):
  def test_emits_until_node_carrying_reserved_latch_var(self):
    doc = json.loads(new_p1().to_json())
    stmts = doc["module"]["stmts"]
    node = next(s for s in stmts if s.get("kind") == "until")
    # P1 allocates frame (id 0), count (id 1), then the latch at the
    # until() call (id 2), exactly as the old desugar did.
    self.assertEqual(node["latch_var"], 2)
    self.assertEqual(len(doc["module"]["vars"]), 3)
    self.assertEqual(
      [(v["id"], v["init"]) for v in doc["module"]["vars"]],
      [(0, 0), (1, 0), (2, 1)],
    )

  def test_p1_byte_identical(self):
    self.assertEqual(
      run_dump_ir(new_p1().to_json()), run_dump_ir(old_p1().to_json())
    )

  def test_p2_byte_identical_later_dword_var(self):
    def new_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def body():
        count.iadd(1)

      until(frame.eq(2), body)
      later = Var(0)
      later.iadd(1)

    def old_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)
      active = Var(1)

      def latch_off():
        active.set(0)

      def body():
        count.iadd(1)

      when(active.eq(1), lambda: when(frame.eq(2), latch_off))
      when(active.eq(1), body)
      later = Var(0)
      later.iadd(1)

    self.assertEqual(
      run_dump_ir(build_module(new_make).to_json()),
      run_dump_ir(build_module(old_make).to_json()),
    )

  def test_p3_byte_identical_body_dword_var(self):
    def new_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def body():
        tmp = Var(0)
        tmp.iadd(1)
        count.iadd(1)

      until(frame.eq(2), body)

    def old_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)
      active = Var(1)

      def latch_off():
        active.set(0)

      def body():
        tmp = Var(0)
        tmp.iadd(1)
        count.iadd(1)

      when(active.eq(1), lambda: when(frame.eq(2), latch_off))
      when(active.eq(1), body)

    self.assertEqual(
      run_dump_ir(build_module(new_make).to_json()),
      run_dump_ir(build_module(old_make).to_json()),
    )

  def test_p4_byte_identical_nested_in_when(self):
    def new_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def inner():
        def body():
          count.iadd(1)

        until(frame.eq(3), body)

      when(frame.eq(1), inner)

    def old_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def inner():
        active = Var(1)

        def latch_off():
          active.set(0)

        def body():
          count.iadd(1)

        when(active.eq(1), lambda: when(frame.eq(3), latch_off))
        when(active.eq(1), body)

      when(frame.eq(1), inner)

    self.assertEqual(
      run_dump_ir(build_module(new_make).to_json()),
      run_dump_ir(build_module(old_make).to_json()),
    )

  def test_nesting_inside_once_and_while_loop(self):
    def new_once_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def outer():
        def body():
          count.iadd(1)

        until(frame.eq(3), body)

      once(frame.eq(1), outer)

    def old_once_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def outer():
        active = Var(1)

        def latch_off():
          active.set(0)

        def body():
          count.iadd(1)

        when(active.eq(1), lambda: when(frame.eq(3), latch_off))
        when(active.eq(1), body)

      once(frame.eq(1), outer)

    self.assertEqual(
      run_dump_ir(build_module(new_once_make).to_json()),
      run_dump_ir(build_module(old_once_make).to_json()),
    )

    def new_while_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def outer():
        def body():
          count.iadd(1)

        until(frame.eq(3), body)

      while_loop(frame.eq(1), outer)

    def old_while_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def outer():
        active = Var(1)

        def latch_off():
          active.set(0)

        def body():
          count.iadd(1)

        when(active.eq(1), lambda: when(frame.eq(3), latch_off))
        when(active.eq(1), body)

      while_loop(frame.eq(1), outer)

    self.assertEqual(
      run_dump_ir(build_module(new_while_make).to_json()),
      run_dump_ir(build_module(old_while_make).to_json()),
    )

  def test_p5_byte_identical_later_unused_bit_var(self):
    def new_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def body():
        count.iadd(1)

      until(frame.eq(2), body)
      later = Var(0)
      _ = later

    def direct_old_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)
      active = Var(1)

      def latch_off():
        active.set(0)

      def body():
        count.iadd(1)

      when(active.eq(1), lambda: when(frame.eq(2), latch_off))
      when(active.eq(1), body)
      later = Var(0)
      _ = later

    def swapped_old_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)
      later = Var(0)
      _ = later
      active = Var(1)

      def latch_off():
        active.set(0)

      def body():
        count.iadd(1)

      when(active.eq(1), lambda: when(frame.eq(2), latch_off))
      when(active.eq(1), body)

    new_lir = run_dump_ir(build_module(new_make).to_json())
    self.assertEqual(new_lir, run_dump_ir(build_module(direct_old_make).to_json()))
    self.assertNotEqual(new_lir, run_dump_ir(build_module(swapped_old_make).to_json()))

  def test_p6_byte_identical_later_boolean_var(self):
    def new_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)

      def body():
        count.iadd(1)

      until(frame.eq(2), body)
      flag = Var(0)

      def flag_body():
        flag.set(0)

      when(flag.eq(1), flag_body)

    def direct_old_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)
      active = Var(1)

      def latch_off():
        active.set(0)

      def body():
        count.iadd(1)

      when(active.eq(1), lambda: when(frame.eq(2), latch_off))
      when(active.eq(1), body)
      flag = Var(0)

      def flag_body():
        flag.set(0)

      when(flag.eq(1), flag_body)

    def swapped_old_make():
      frame = Var(0)
      count = Var(0)
      frame.iadd(1)
      flag = Var(0)
      active = Var(1)

      def latch_off():
        active.set(0)

      def body():
        count.iadd(1)

      when(active.eq(1), lambda: when(frame.eq(2), latch_off))
      when(active.eq(1), body)

      def flag_body():
        flag.set(0)

      when(flag.eq(1), flag_body)

    new_lir = run_dump_ir(build_module(new_make).to_json())
    self.assertEqual(new_lir, run_dump_ir(build_module(direct_old_make).to_json()))
    self.assertNotEqual(new_lir, run_dump_ir(build_module(swapped_old_make).to_json()))


if __name__ == "__main__":
  unittest.main()
