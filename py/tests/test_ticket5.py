import unittest

from py import (
    AtLeast,
    AtMost,
    CPCursor,
    CSprite,
    CUnit,
    Deaths,
    DeathsX,
    EPD,
    EPD_BASE,
    EPD_CURRENT_PLAYER,
    EudslError,
    Exactly,
    MemCond,
    MemView,
    SetDeaths,
    SetDeathsX,
    SetTo,
    Trigger,
    Var,
    build_module,
    defineMemView,
    while_loop,
    withCP,
    with_cp,
)

from py.tests.helpers import run_sim
class Ticket5Tests(unittest.TestCase):
    def test_cunit_and_csprite_schemas_load(self):
        u = CUnit(1000)
        s = CSprite(2000)

        # CUnit size: 336 bytes (84 dwords)
        self.assertEqual(u.schema.name, "CUnit")
        self.assertEqual(u.schema.size, 336)

        # CSprite size: 36 bytes (9 dwords)
        self.assertEqual(s.schema.name, "CSprite")
        self.assertEqual(s.schema.size, 36)

        # CUnit fields
        self.assertEqual(u.hp.byteOffset, 8)
        self.assertEqual(u.hp.size, 4)
        self.assertEqual(u.hp.epd, 1002)

        self.assertEqual(u.posX.byteOffset, 40)
        self.assertEqual(u.posX.size, 2)
        self.assertEqual(u.posX.shift, 0)
        self.assertEqual(u.posX.bitMask, 0x0000FFFF)
        self.assertEqual(u.posX.epd, 1010)

        self.assertEqual(u.posY.byteOffset, 42)
        self.assertEqual(u.posY.size, 2)
        self.assertEqual(u.posY.shift, 16)
        self.assertEqual(u.posY.bitMask, 0xFFFF0000)
        self.assertEqual(u.posY.epd, 1010)

        # CSprite fields
        self.assertEqual(s.index.byteOffset, 16)
        self.assertEqual(s.index.size, 2)
        self.assertEqual(s.index.epd, 2004)

    def test_property_access_aliases_and_normalization(self):
        u = CUnit(1000)

        # hp aliases
        self.assertEqual(u.hp.byteOffset, 8)
        self.assertEqual(u.hitPoints.byteOffset, 8)
        self.assertEqual(u.hit_points.byteOffset, 8)

        # shield aliases
        self.assertEqual(u.shield.byteOffset, 96)
        self.assertEqual(u.shields.byteOffset, 96)

        # order aliases
        self.assertEqual(u.order.byteOffset, 77)
        self.assertEqual(u.orderID.byteOffset, 77)
        self.assertEqual(u.order_id.byteOffset, 77)

        # owner aliases
        self.assertEqual(u.owner.byteOffset, 76)
        self.assertEqual(u.playerID.byteOffset, 76)
        self.assertEqual(u.player_id.byteOffset, 76)

        # posX / posY normalization
        self.assertEqual(u.posX.byteOffset, 40)
        self.assertEqual(u.pos_x.byteOffset, 40)
        self.assertEqual(u.posY.byteOffset, 42)
        self.assertEqual(u.pos_y.byteOffset, 42)

        # Unknown property throws EUDSL-BOUNDS
        with self.assertRaises(EudslError):
            _ = u.nonExistentProperty

    def test_constant_epd_mutations_and_comparisons(self):
        base = 5000
        u = CUnit(base)

        # .set() emits 1 SetDeathsX action
        act_set = u.hp.act_set(1500)
        self.assertEqual(act_set["acttype"], 45)
        self.assertEqual(act_set["modifier"], 7)  # SetTo
        self.assertEqual(act_set["dest"], base + 2)
        self.assertEqual(act_set["value"], 1500)
        self.assertEqual(act_set["mask"], 0xFFFFFFFF)
        self.assertIsNone(u.hp.set(1500))

        # Sub-dword .act_set() masks and shifts
        act_pos_y = u.posY.act_set(300)
        self.assertEqual(act_pos_y["dest"], base + 10)
        self.assertEqual(act_pos_y["value"], 300 << 16)
        self.assertEqual(act_pos_y["mask"], 0xFFFF0000)

        # Comparisons emit 1C DeathsX conditions
        cond_ge = u.hp.ge(500)
        self.assertEqual(cond_ge["condtype"], 15)
        self.assertEqual(cond_ge["comparison"], 0)  # AtLeast
        self.assertEqual(cond_ge["player"], base + 2)
        self.assertEqual(cond_ge["amount"], 500)

        # Python operator overloading: u.hp == 1500
        cond_eq = u.hp == 1500
        self.assertEqual(cond_eq["condtype"], 15)
        self.assertEqual(cond_eq["comparison"], 10)  # Exactly
        self.assertEqual(cond_eq["amount"], 1500)

        cond_eq_sub = u.posY.eq(300)
        self.assertEqual(cond_eq_sub["player"], base + 10)
        self.assertEqual(cond_eq_sub["comparison"], 10)
        self.assertEqual(cond_eq_sub["amount"], 300 << 16)
        self.assertEqual(cond_eq_sub["locid"], 0xFFFF0000)

        # MemCond free function
        mc1 = MemCond(base + 2, "Exactly", 1500)
        self.assertEqual(mc1["condtype"], 15)
        self.assertEqual(mc1["comparison"], 10)
        self.assertEqual(mc1["amount"], 1500)

        mc2 = MemCond(base + 10, AtLeast, 200 << 16, 0xFFFF0000)
        self.assertEqual(mc2["condtype"], 15)
        self.assertEqual(mc2["comparison"], 0)
        self.assertEqual(mc2["amount"], 200 << 16)
        self.assertEqual(mc2["locid"], 0xFFFF0000)

    def test_condview_and_actview_subfields(self):
        t0 = Trigger(
            conds=[Deaths(10, Exactly, 100, 0)],
            acts=[SetDeaths(20, SetTo, 200, 0)],
        )

        # CondView slot 0
        c0 = t0.cond(0)
        act_cond_amt = c0.amount.set(500)
        self.assertEqual(act_cond_amt["acttype"], 45)
        self.assertEqual(act_cond_amt["value"], 500)

        cond_check = c0.amount.ge(250)
        self.assertEqual(cond_check["condtype"], 15)
        self.assertEqual(cond_check["comparison"], 0)  # AtLeast
        self.assertEqual(cond_check["amount"], 250)

        # Sub-dword cond field (unit is 2 bytes at offset 12)
        act_cond_unit = c0.unit.set(84)
        self.assertEqual(act_cond_unit["mask"], 0x0000FFFF)
        self.assertEqual(act_cond_unit["value"], 84)

        # ActView slot 0
        a0 = t0.act(0)
        act_val = a0.value.set(999)
        self.assertEqual(act_val["acttype"], 45)
        self.assertEqual(act_val["value"], 999)

        # Sub-dword act field (modifier is 1 byte at offset 27, shift = 24)
        act_mod = a0.modifier.set(8)  # Add = 8
        self.assertEqual(act_mod["mask"], 0xFF000000)
        self.assertEqual(act_mod["value"], 8 << 24)

    def test_with_cp_scheduling_and_restoration(self):
        base = 7000

        def prog():
            with with_cp(base) as cp:
                # 1. cp.at(2): writes 100 via Player 13 (CurrentPlayer) at offset +2
                cp.at(2).set(100)

                # 2. cp.view(CUnit): hp is at dword +2 (delta = 0, no extra hop!)
                u = cp.view(CUnit)
                u.hp.iadd(50)

                # 3. shield is at dword +24 (delta = +22 hop)
                u.shield.set(800)

                # 4. posX is at dword +10 (delta = -14 hop, sub-dword low word)
                u.posX.set(42)

        mod = build_module(prog)
        report = run_sim(mod.to_json())
        self.assertTrue(report["self_targeted_invariant_holds"])
        epds = report.get("external_epds", {})

        # EPD 7002 (hp): 100 + 50 = 150
        self.assertEqual(epds["7002"], 150)
        # EPD 7024 (shield): 800
        self.assertEqual(epds["7024"], 800)
        # EPD 7010 (posX): 42 in low word
        self.assertEqual(epds["7010"] & 0xFFFF, 42)

        # CurrentPlayer (EPD 203155) cleanly restored back to base (7000)
        self.assertEqual(epds["203155"], base)

    def test_with_cp_hop_inside_loop_is_absolute(self):
        ids = {}

        def prog():
            n = Var(2)
            acc = Var(0)
            SetDeaths(5001, SetTo, 0x1234, 0)
            SetDeaths(5002, SetTo, 0x5678, 0)
            with with_cp(5000) as cp:

                def body():
                    # A non-zero offset hop replayed on every pass must
                    # re-anchor at base + 1, not advance the register.
                    acc.iadd(cp.at(1).read())
                    n.isub(1)

                while_loop([n.ge(1)], body)
            ids["acc"] = acc.id

        report = run_sim(build_module(prog).to_json(), 1)
        # Both passes read EPD 5001: 0x1234 + 0x1234, never 5002.
        self.assertEqual(report["var_values"][ids["acc"]], 0x2468)
        # CurrentPlayer (EPD 203155) is re-anchored to the base at exit.
        self.assertEqual(report["external_epds"]["203155"], 5000)

    def test_with_cp_rejects_dynamic_base(self):
        # The guard fires at with_cp entry, before any act is queued, so a
        # non-int base never reaches the builder's act queue.
        with self.assertRaises(EudslError) as caught:
            with_cp(object())
        self.assertEqual(caught.exception.code, "EUDSL-TYPE")
        self.assertIn("base", str(caught.exception))

        # A Var base is the realistic dynamic case. Constructing one needs a
        # builder, but the guard still fires before the seed act is queued.
        def prog():
            with with_cp(Var(5000)):
                pass

        with self.assertRaises(EudslError) as caught:
            build_module(prog)
        self.assertEqual(caught.exception.code, "EUDSL-TYPE")
        self.assertIn("base", str(caught.exception))

        # Defense in depth: the cursor itself refuses a non-int base.
        with self.assertRaises(EudslError) as caught:
            CPCursor(object())
        self.assertEqual(caught.exception.code, "EUDSL-TYPE")

    def test_trig_fire_runtime_mutation(self):
        def prog():
            v = Var(10)
            gate = Var(0)

            # Target routine with initially unmet condition (gate == 1)
            routine = Trigger(
                conds=[gate.eq(1)],
                acts=[v.act_add(50)],
                preserved=True,
            )

            # Mutate condition and action via CondView and ActView at runtime
            routine.cond(0).amount.set(2)
            routine.act(0).value.set(100)

            # Code before fire
            v.iadd(5)
            gate.set(2)  # gate is now 2, matching mutated condition!

            # Fire routine
            routine.fire()

            # Code after fire
            v.iadd(1)

        mod = build_module(prog)
        report = run_sim(mod.to_json())
        self.assertTrue(report["self_targeted_invariant_holds"])
        # 10 + 5 + 100 + 1 = 116
        self.assertEqual(report["var_values"][0], 116)

    def test_read_memory_into_var(self):
        base = 8000
        ids: dict[str, int] = {}

        def prog():
            u = CUnit(base)
            u.hp.set(777)
            read_hp = u.hp.read()
            result = Var(0)
            result.set(read_hp)
            ids["read_hp"] = read_hp.id
            ids["result"] = result.id

        mod = build_module(prog)
        report = run_sim(mod.to_json())
        self.assertTrue(report["self_targeted_invariant_holds"])
        # The optimizer fuses a call result into its single-use destination
        # (#97), so the value lands directly in `result`; the temporary may
        # stay untouched.
        self.assertEqual(report["var_values"][ids["result"]], 777)

if __name__ == "__main__":
    unittest.main()
