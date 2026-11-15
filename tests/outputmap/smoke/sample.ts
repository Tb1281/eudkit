/**
 * Decimal HUD sample map (ADR 0014, issue #81).
 *
 * Writes `wave=   5 hp= 100` with invisible 0x1A leading pads:
 *
 *   wave=\x1a\x1a\x1a5 hp=\x1a100
 *
 * 0x1A is zero-width in StarCraft: Remastered, so the displayed line reads
 * `wave=5 hp=100`.
 *
 * Fixed-width decimal `{:\x1a>4d}` keeps compile-time byte offsets. The
 * re-baselined pre-assembly cost for this sample (ADR 0014, issue #98) is
 * 100T static / 105T executed (51T / 53T for one placeholder in isolation).
 * Issue #98 removed the per-digit scratch-trigger `emitVarStore` pointer
 * patching: each digit byte is now a masked `SetStringDword` action at its
 * compile-time dword and lane, with no runtime `pos.shr(2)`.
 */
import { FixedString, Var, buildModule } from "../../../ts/index.ts";

export const module = buildModule(() => {
  const hud = new FixedString(32);
  const wave = Var(5);
  const hp = Var(100);
  hud.write("wave={:\x1a>4d} hp={:\x1a>4d}", wave, hp);
  hud.display();
});
