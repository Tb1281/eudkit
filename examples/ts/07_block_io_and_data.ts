/**
 * Topic 07 (eudkit TypeScript): Block Copy, Fill, and Payload Injection.
 *
 * Migration notes (eudplib -> eudkit):
 * - `f_repmovsd_epd(dst, src, dwords)` -> `memcpyEPD(dstEpd, srcEpd, dwords)`:
 *   eudkit borrows `CurrentPlayer` (`0x6509B0`) to read `srcEpd` and leaves
 *   `CurrentPlayer == srcEpd + count - 1`. `memcpyEPD` also accepts a `Db` blob
 *   directly as its source via a mode-1 relocation (`memcpyEPD(dstEpd, blob, dwords)`).
 * - `EUDWhile` + `f_dwwrite_epd` -> `memsetEPD(dstEpd, value, count)`:
 *   constant `(dstEpd, value, count)` fills with `ceil(count / 64) <= 512` chunks
 *   fold into packed `SetDeaths` triggers (84 dwords -> 2 triggers, no loop),
 *   while a runtime `Var` count lowers to a check-first `whileLoop`.
 * - euddraft `_add_datadumper` / `dataDumper` plugin -> `dataDumper(Uint8Array, [addr], ["copy"])`:
 *   eudkit has no plugin split or unpatch stack (`f_dwpatch_epd`/`f_unpatchall`);
 *   a single `dataDumper` call allocates a dword-padded `Db` blob and emits the
 *   `once`-gated copy routine (note TS takes `Uint8Array`, Py accepts `bytes`).
 *
 * Note on imports:
 *   In external npm packages using `"eudkit"`, import from `"eudkit"`:
 *   `import { CUnit, Db, DisplayText, EPD, ... } from "eudkit";`
 *
 * WARNING — scratch slot 1699 is NOT established safe scratch. This example
 * assumes the repo's 1699-slot CUnit model (`ts/view.ts: CUNIT_SLOT_COUNT =
 * 1699`, slots 0..1698, pool end `0x628297`), so slot 1699 looks like free
 * space past the pool. External sources contradict that model: BWAPI accepts
 * unit indices `0 <= unitIndex < 1700`
 * (https://bwapi.github.io/class_b_w_a_p_i_1_1_game.html), and the euddraft wiki
 * states 1700 units (0th–1699th) span `0x59CCA8`–`0x6283E7`
 * (https://github-wiki-see.page/m/phu54321/euddraft/wiki/05.-Selecting-a-pawn).
 * Under those sources slot 1699 (`0x628298`–`0x6283E7`) is the last VALID
 * CUnit, and the 84-dword copy below overwrites it when occupied. Do not copy
 * this scratch choice into a real map until the 1699-vs-1700 pool size is
 * settled at the code level; the address is kept here only so the shipped
 * example still matches its eudplib baseline.
 */

import {
  CUnit,
  Db,
  DisplayText,
  EPD,
  EPD_CURRENT_PLAYER,
  SetDeaths,
  SetMemory,
  SetTo,
  Var,
  buildModule,
  dataDumper,
  memcpyEPD,
  memsetEPD,
  once,
  when,
} from "../../ts/index.ts";

const CUNIT_POOL_ADDR = 0x59cca8;
const CUNIT_SIZE = 336;
const CUNIT_DWORDS = 84;

const SLOT0_ADDR = CUNIT_POOL_ADDR;
// WARNING: slot 1699 is the last VALID CUnit under BWAPI (1700 slots) — see
// the module docstring. This address is kept only to match the eudplib
// baseline; do not treat it as safe scratch in a real map.
const SCRATCH_SLOT_ADDR = CUNIT_POOL_ADDR + 1699 * CUNIT_SIZE;

const SRC_EPD = EPD(SLOT0_ADDR);
const SCRATCH_EPD = EPD(SCRATCH_SLOT_ADDR);

const BLOB_DST_ADDR = 0x58e100;
const BLOB_DST_EPD = EPD(BLOB_DST_ADDR);

const DUMPER_PTR_ADDR = 0x58d740;
const DUMPER_DEST_ADDR = 0x58e200;

export const module = buildModule(() => {
  const snapshotHp = Var(0);
  const verified = Var(0);
  const runtimeCount = Var(4);
  const blob = new Db(new TextEncoder().encode("EUDKITV3DATA"));

  once(() => {
    // 1. Seed CUnit slot 0 HP (40 HP = 10240 in 8.8 fixed point) for the demo.
    const srcUnit = CUnit(SRC_EPD);
    srcUnit.hp.set(10240);

    // 2. Snapshot CUnit slot 0 (84 dwords) into scratch slot 1699.
    //    (memcpyEPD advances CurrentPlayer to SRC_EPD + 84 - 1.)
    memcpyEPD(SCRATCH_EPD, SRC_EPD, CUNIT_DWORDS);

    // 3. Verify the snapshotted CUnit's hp field equals 10240.
    const snapUnit = CUnit(SCRATCH_EPD);
    snapshotHp.set(snapUnit.hp.read());
    when([snapshotHp.eq(10240)], () => verified.set(1));

    // 4. Constant-count memsetEPD (84 dwords -> folds to 2 packed SetDeaths triggers).
    memsetEPD(SCRATCH_EPD, 0, CUNIT_DWORDS);

    // 5. Runtime-count memsetEPD (Var count -> lowers to a check-first whileLoop).
    memsetEPD(SCRATCH_EPD, 0x11223344, runtimeCount);

    // 6. Copy a Db blob directly via memcpyEPD (3 dwords = 12 bytes).
    memcpyEPD(BLOB_DST_EPD, blob, 3);

    // 7. Seed the destination pointer that dataDumper("copy") will dereference.
    SetMemory(DUMPER_PTR_ADDR, SetTo, DUMPER_DEST_ADDR);
  });

  // 8. Inject payload and copy it to the address stored at DUMPER_PTR_ADDR.
  //    (Wrapped in `once` internally; tail bytes are zero-padded to a dword.)
  dataDumper(new TextEncoder().encode("HELLO_EUD!"), [DUMPER_PTR_ADDR], ["copy"]);

  // 9. Restore CurrentPlayer (0x6509B0) to P1 (0) after memcpyEPD / dataDumper.
  once(() => {
    SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0);
    when([verified.eq(1)], () => DisplayText("Block IO & DataDumper OK"));
  });
});
