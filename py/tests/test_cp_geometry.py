"""CP page geometry is derived from the trigger layout (FE4).

``SLOT_STRIDE_EPDS``, ``PAGE_SLOTS``, and ``PAGE_HOP_EPDS`` are views of the
``Trigger (16C, 64A)`` layout, not independent literals:

* one action slot is 32 bytes = 8 EPDs (``ACT_STRIDE_EPDS``);
* one ``TriggerPage`` holds 64 slots (``ACT_COUNT``);
* a whole trigger is 602 EPDs, so hopping past the 64 slots leaves
  ``602 - 64 * 8 = 90`` EPDs (``TRIGGER_STRIDE_EPDS - ACT_COUNT *
  ACT_STRIDE_EPDS``).

The pins below fail if ``trigger``'s constants move without ``cp`` following,
or if ``cp`` goes back to restating the numbers.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from py import cp, trigger

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class CPGeometryTest(unittest.TestCase):
    def test_geometry_pins_the_stock_trigger_layout(self) -> None:
        # One 32-byte action slot, 64 slots per TriggerPage, and a 602-EPD
        # trigger stride: 602 - 64 * 8 = 90 EPDs of page header/footer.
        self.assertEqual(cp.SLOT_STRIDE_EPDS, 8)
        self.assertEqual(cp.PAGE_SLOTS, 64)
        self.assertEqual(cp.PAGE_HOP_EPDS, 90)

    def test_geometry_is_expressed_over_the_trigger_constants(self) -> None:
        self.assertEqual(cp.SLOT_STRIDE_EPDS, trigger.ACT_STRIDE_EPDS)
        self.assertEqual(cp.PAGE_SLOTS, trigger.ACT_COUNT)
        self.assertEqual(
            cp.PAGE_HOP_EPDS,
            trigger.TRIGGER_STRIDE_EPDS
            - trigger.ACT_COUNT * trigger.ACT_STRIDE_EPDS,
        )
        # The page hop is "one trigger page minus the slots themselves",
        # expressed through cp's own public names.
        self.assertEqual(
            cp.PAGE_HOP_EPDS,
            trigger.TRIGGER_STRIDE_EPDS - cp.PAGE_SLOTS * cp.SLOT_STRIDE_EPDS,
        )

    def test_changing_the_trigger_constants_flows_through(self) -> None:
        """Patch trigger in a subprocess, reload cp, read the new geometry.

        The reload is isolated so the rest of the suite keeps the real
        modules. A derived ``cp`` prints the patched geometry (``4 32 372``);
        an independent literal would still print ``8 64 90``.
        """
        script = textwrap.dedent(
            """
            import importlib

            import py.cp
            import py.trigger as trigger

            trigger.ACT_STRIDE_EPDS = 4
            trigger.ACT_COUNT = 32
            trigger.TRIGGER_STRIDE_EPDS = 500
            cp = importlib.reload(py.cp)
            print(cp.SLOT_STRIDE_EPDS, cp.PAGE_SLOTS, cp.PAGE_HOP_EPDS)
            """
        )
        out = subprocess.check_output(
            [sys.executable, "-c", script], cwd=REPO_ROOT, text=True
        )
        self.assertEqual(out.strip(), "4 32 372")


if __name__ == "__main__":
    unittest.main()
