/**
 * Topic 07 (eudkit TypeScript): Block Copy, Fill, and Payload Injection.
 *
 * Migration notes (eudplib -> eudkit):
 * - `f_repmovsd_epd(dst, src, dwords)` -> `memcpyEPD(dstEpd, srcEpd, dwords)`:
 *   eudkit borrows `CurrentPlayer` (`0x6509B0`) to read `srcEpd` and leaves
 *   `CurrentPlayer == srcEpd + count - 1`. `memcpyEPD` also accepts a `Db` blob
 *   directly as its source via a mode-1 relocation (`memcpyEPD(dstEpd, blob, dwords)`).
 * - `EUDWhile` + `f_dwwrite_epd` -> `memsetEPD(dstEpd, value, count)`:
 *   a constant `(dstEpd, value, count)` fill folds into packed `SetDeaths`
 *   triggers (84 dwords -> 2 triggers, no loop) — but only when `dstEpd` is a
 *   constant. This example's scratch EPD is a runtime `Var` (see below), so
 *   its fills lower to the check-first `whileLoop` instead; the fold itself is
 *   covered by unit tests (`ts/tests/test_memory_costs.ts`).
 * - euddraft `_add_datadumper` / `dataDumper` plugin -> `dataDumper(Uint8Array, [addr], ["copy"])`:
 *   eudkit has no plugin split or unpatch stack (`f_dwpatch_epd`/`f_unpatchall`);
 *   a single `dataDumper` call allocates a dword-padded `Db` blob and emits the
 *   `once`-gated copy routine (note TS takes `Uint8Array`, Py accepts `bytes`).
 *
 * Scratch buffer:
 *   The 84-dword snapshot target is a 336-byte payload `Db` blob, not a CUnit
 *   slot. `epdOfDb(blob)` returns a `Var` holding the blob's assembly-time
 *   EPD, which `memcpyEPD` / `memsetEPD` accept as a runtime destination and
 *   `CUnitView` accepts as a runtime EPD. The blob is compiler-owned payload
 *   memory, so the copy can never overwrite a live unit. Slot 1699 must never
 *   be used as scratch: the pool has 1700 slots
 *   (`ts/view.ts: CUNIT_SLOT_COUNT = 1700`), so slot 1699 is the last VALID
 *   CUnit (BWAPI: unit indices `0 <= i < 1700`; euddraft wiki: 1700 units
 *   spanning `0x59cca8`–`0x6283e7`).
 *
 * Note on imports:
 *   In external npm packages using `"eudkit"`, import from `"eudkit"`:
 *   `import { CUnit, Db, DisplayText, EPD, ... } from "eudkit";`
 */

import {
  CUnit,
  CUnitView,
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
  epdOfDb,
  memcpyEPD,
  memsetEPD,
  once,
  when,
} from "../../ts/index.ts";

const CUNIT_POOL_ADDR = 0x59cca8;
const CUNIT_SIZE = 336;
const CUNIT_DWORDS = 84;

const SLOT0_ADDR = CUNIT_POOL_ADDR;

const SRC_EPD = EPD(SLOT0_ADDR);

const BLOB_DST_ADDR = 0x58e100;
const BLOB_DST_EPD = EPD(BLOB_DST_ADDR);

const DUMPER_PTR_ADDR = 0x58d740;
const DUMPER_DEST_ADDR = 0x58e200;

export const module = buildModule(() => {
  const snapshotHp = Var(0);
  const verified = Var(0);
  const runtimeCount = Var(4);
  const blob = new Db(new TextEncoder().encode("EUDKITV3DATA"));
  // 336-byte payload scratch: compiler-owned memory, never a live unit.
  // Its assembly-time EPD arrives as a runtime Var (see module docstring).
  const scratch = new Db(CUNIT_DWORDS * 4);
  const scratchEpd = epdOfDb(scratch);

  once(() => {
    // 1. Seed CUnit slot 0 HP (40 HP = 10240 in 8.8 fixed point) for the demo.
    const srcUnit = CUnit(SRC_EPD);
    srcUnit.hp.set(10240);

    // 2. Snapshot CUnit slot 0 (84 dwords) into the payload scratch blob.
    //    (memcpyEPD advances CurrentPlayer to SRC_EPD + 84 - 1.)
    memcpyEPD(scratchEpd, SRC_EPD, CUNIT_DWORDS);

    // 3. Verify the snapshotted CUnit's hp field equals 10240. The field
    //    handle (`.hp.read()`) only accepts a constant EPD, so a runtime EPD
    //    uses the `CUnitView` `read("hp")` (via `withEpd`) instead.
    const snapUnit = new CUnitView(scratchEpd);
    snapshotHp.set(snapUnit.read("hp"));
    when([snapshotHp.eq(10240)], () => verified.set(1));

    // 4. Constant-count memsetEPD at a runtime EPD (84 dwords -> check-first
    //    whileLoop, not the packed SetDeaths fold, which needs a constant dst).
    memsetEPD(scratchEpd, 0, CUNIT_DWORDS);

    // 5. Runtime-count memsetEPD (Var count -> lowers to a check-first whileLoop).
    memsetEPD(scratchEpd, 0x11223344, runtimeCount);

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
