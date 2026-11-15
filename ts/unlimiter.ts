/**
 * The unlimiter: a core helper port of euddraft's `plugins/unlimiter.py`
 * (issue #94). Python mirror: `py/unlimiter.py`.
 *
 * The plugin replaces four grow-only game lists (bullets, sprites, images and
 * orders) with lists backed by module data blobs, so the game can keep
 * allocating entries past its vanilla caps. `connectDList(elStart, elEnd,
 * itemSize, itemN, isSprite)` builds one such list on the first trigger loop,
 * and the helper's last action rewrites one dword every loop.
 *
 * This module is a regular core helper: no plugin system, no settings manifest
 * and no lifecycle hook. One `unlimiter()` call inside `buildModule` arms the
 * four lists for the compiled module; `connectDList` is the underlying
 * primitive for the rare caller that wants one list only.
 *
 * List layout
 * -----------
 * Each element is a node of a doubly-linked list in StarCraft's node order:
 * `prev` at `+0` and `next` at `+4` (the same order `CUnit` / `CSprite` use).
 * For `n` elements at stride `size` starting at element 0:
 *
 * - `element[k].prev = element[k - 1]` for `k >= 1` and `element[0].prev = 0`;
 * - `element[k].next = element[k + 1]` for `k < n - 1` and
 *   `element[n - 1].next = 0`;
 * - `elStart` points at element 0, `elEnd` at `element[n - 1]`.
 *
 * The four lists are pinned as module constants:
 *
 * | list    | head     | tail     | stride | elements |
 * | ------- | -------- | -------- | ------ | -------- |
 * | bullets | 0x64EED8 | 0x64EEDC | 112    | 8192     |
 * | sprites | 0x63FE30 | 0x63FE34 | 36     | 65536    |
 * | images  | 0x57EB68 | 0x57EB70 | 64     | 65536    |
 * | orders  | 0x64B2E0 | 0x64B2E4 | 20     | 65536    |
 *
 * Sprite elements also get `+0x10 = 10000`; the source patches that action's
 * value by `+1` per element, so the field runs `10000, 10001, ...` in list
 * order. The four blobs total 8,782,288 bytes of zero-filled payload
 * (8.38 MiB): the helper is a payload-size cost as much as a trigger cost.
 *
 * How the loop is ported
 * ----------------------
 * `EUDLoopN()(itemN)` is a *runtime* loop: the source emits one body and
 * advances its own `SetMemory` actions by patching the action's `dest` (`+16`,
 * an EPD delta of `itemSize / 4`) and `value` (`+20`, a byte address delta of
 * `itemSize`) after each write. The port keeps that shape with `whileLoop` and
 * eudkit's action-slot references: `Trigger.act()` exposes `act(k).dest` /
 * `.value` as relocatable fields, and `SetMemoryEPD` (the field-reference form
 * of the Deaths write) emits the same additive patches. The source puts the
 * writes and their own patches in one `DoActions` trigger, so a single
 * evaluation writes element *k* and then advances the actions for element
 * *k + 1*. eudkit action slots are only addressable once the trigger exists, so
 * the port emits the same iterations as back-to-back triggers: write trigger,
 * optional sprite trigger, one shared patch block, then the loop check. The
 * write/patch order per element is unchanged; the split costs one extra trigger
 * evaluation per element (accounted for in the cost model below).
 *
 * One deviation is forced by eudkit's HIR:
 *
 * - **One padding element on each side of the list.** The source seeds element
 *   0's `prev` with `dataDb - itemSize` (one element *before* the blob) and
 *   the last element's `next` with one element *after* it. eudkit blob
 *   relocations are non-negative offsets, so the blob reserves a padding
 *   element on both sides (`itemSize * (itemN + 2)` bytes) and the list starts
 *   at `db.at(itemSize)`: both seeds address in-blob padding, and the two
 *   dangling pointers are still overwritten with 0 by the final patch, exactly
 *   as in the source. No read ever observes a padding element.
 *
 * Arming must happen inside a single loop pass: the patches that advance the
 * write actions only take effect on the *next* evaluation of the write
 * trigger, and the tail zeroes the two dangling pointers right after the loop.
 * Both hold for the source's `EUDLoopN` too — the loop is intra-pass, not
 * spread across frames.
 *
 * Because the pass is long, `eudkit dump-ir` (which simulates one loop by
 * default) trips the simulator's 100,000-evaluation guard on a module that
 * arms the helper; use `dump-ir --no-exec` for the static report or
 * `compile --check-cost`. An unlimiter-only module is 22T / 4C / 64A with
 * 8,782,304 blob bytes (8,782,288 payload plus the four-entry blob table).
 *
 * The per-loop `SetMemory(0x64DEBC, SetTo, 40)` is emitted as a top-level
 * action by `unlimiter()`; call it after the module's other statements so the
 * write stays last, matching `afterTriggerExec`.
 *
 * Cost
 * ----
 * Static cost is tiny (four loop counters, the gate/check pairs, three or four
 * triggers per element shape and the tails), while arming the helper costs one
 * trigger loop pass: 679,936 evaluations (204,800 loop checks + 204,800 write
 * triggers + 204,800 patch blocks + 65,536 sprite write triggers). That is one
 * extra trigger evaluation per element over the source's combined write+patch
 * trigger; the payload bytes above are the part that shows up in the map file.
 */

import { Add, Db, SetMemory, SetMemoryEPD, SetTo } from "./hir.ts";
import { once, whileLoop } from "./temporal.ts";
import { Trigger } from "./trigger.ts";
import { Var } from "./var.ts";
import { EudslError, requirePlainInt } from "./view.ts";

/** Bullet list head/tail dwords. */
export const BULLET_LIST_ADDR = 0x64eed8;
export const BULLET_LIST_END_ADDR = 0x64eedc;
export const BULLET_ITEM_SIZE = 112;
export const BULLET_ITEM_COUNT = 8192;

/** Sprite list head/tail dwords. */
export const SPRITE_LIST_ADDR = 0x63fe30;
export const SPRITE_LIST_END_ADDR = 0x63fe34;
export const SPRITE_ITEM_SIZE = 36;
export const SPRITE_ITEM_COUNT = 65536;
/** Sprite element field the plugin seeds with {@link SPRITE_FIELD_SEED}. */
export const SPRITE_FIELD_OFFSET = 0x10;
export const SPRITE_FIELD_SEED = 10000;

/** Image list head/tail dwords. */
export const IMAGE_LIST_ADDR = 0x57eb68;
export const IMAGE_LIST_END_ADDR = 0x57eb70;
export const IMAGE_ITEM_SIZE = 64;
export const IMAGE_ITEM_COUNT = 65536;

/** Order list head/tail dwords. */
export const ORDER_LIST_ADDR = 0x64b2e0;
export const ORDER_LIST_END_ADDR = 0x64b2e4;
export const ORDER_ITEM_SIZE = 20;
export const ORDER_ITEM_COUNT = 65536;

/** Dword the plugin rewrites on every trigger loop in `afterTriggerExec`. */
export const PER_TICK_ADDR = 0x64debc;
/** Value written to {@link PER_TICK_ADDR} every loop. */
export const PER_TICK_VALUE = 40;

/** Fields of one linked-list node: `prev` at `+0`, `next` at `+4`. */
export const PREV_FIELD_OFFSET = 0;
export const NEXT_FIELD_OFFSET = 4;
/** Smallest node stride that holds both link fields. */
export const MIN_ITEM_SIZE = NEXT_FIELD_OFFSET + 4;

/** The four data blobs backing the connected game lists. */
export interface UnlimitedLists {
  readonly bullets: Db;
  readonly sprites: Db;
  readonly images: Db;
  readonly orders: Db;
}

function requireListAddr(name: string, value: unknown): number {
  const addr = requirePlainInt(value, `connectDList(): ${name}`);
  if (addr <= 0 || addr > 0xfffffffc || addr % 4 !== 0) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `connectDList(): ${name} must be a positive 4-byte-aligned address, got ${addr}`,
    );
  }
  return addr;
}

function requireItemSize(value: unknown, isSprite: boolean): number {
  const size = requirePlainInt(value, "connectDList(): itemSize");
  const minimum = isSprite ? Math.max(MIN_ITEM_SIZE, SPRITE_FIELD_OFFSET + 4) : MIN_ITEM_SIZE;
  if (size < minimum || size % 4 !== 0 || size > 0xffff) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `connectDList(): itemSize must be a multiple of 4 in [${minimum}, ${0xffff}], got ${size}`,
    );
  }
  return size;
}

function requireItemCount(value: unknown): number {
  const count = requirePlainInt(value, "connectDList(): itemN");
  if (count < 1 || count > 0xffffffff) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `connectDList(): itemN must be in [1, ${0xffffffff}], got ${count}`,
    );
  }
  return count;
}

function requireSpriteFlag(value: unknown): boolean {
  const flag = requirePlainInt(value, "connectDList(): isSprite");
  if (flag !== 0 && flag !== 1) {
    throw new EudslError(
      "EUDSL-BOUNDS",
      `connectDList(): isSprite must be 0 or 1, got ${flag}`,
    );
  }
  return flag === 1;
}

/**
 * Build one unlimiter list in a data blob and patch its head/tail.
 *
 * `elStart`/`elEnd` are the game's list head and tail dwords, `itemSize` the
 * node stride in bytes, `itemN` the element count and `isSprite` selects the
 * sprite variant (an extra `+0x10 = 10000` field per element, incremented per
 * element). The body runs once, on the first trigger loop (eudplib's
 * `onPluginStart`), and returns the blob holding the list.
 */
export function connectDList(
  elStart: number,
  elEnd: number,
  itemSize: number,
  itemN: number,
  isSprite = 0,
): Db {
  const spriteFlag = requireSpriteFlag(isSprite);
  const start = requireListAddr("elStart", elStart);
  const end = requireListAddr("elEnd", elEnd);
  const size = requireItemSize(itemSize, spriteFlag);
  const count = requireItemCount(itemN);

  const stride = size / 4;
  // One padding element before and after the list; see the module docstring.
  const data = new Db(size * (count + 2));
  const base = data.at(size);
  const last = data.at(size * count);

  once(() => {
    const counter = Var(count);

    const body = (): void => {
      const writes = new Trigger({
        conds: [],
        acts: [
          SetMemory(base.at(PREV_FIELD_OFFSET), SetTo, data),
          SetMemory(base.at(NEXT_FIELD_OFFSET), SetTo, data.at(2 * size)),
        ],
      });
      // The source emits the sprite write before the patch block, and a
      // trigger's slots are only addressable after it exists, so the sprite
      // write trigger is created here too. All patches then share the next
      // trigger of this iteration (see the module docstring).
      let sprite: Trigger | undefined;
      if (spriteFlag) {
        sprite = new Trigger({
          conds: [],
          acts: [SetMemory(base.at(SPRITE_FIELD_OFFSET), SetTo, SPRITE_FIELD_SEED)],
        });
      }

      SetMemoryEPD(writes.act(0).dest, Add, stride);
      SetMemoryEPD(writes.act(0).value, Add, size);
      SetMemoryEPD(writes.act(1).dest, Add, stride);
      SetMemoryEPD(writes.act(1).value, Add, size);
      if (sprite !== undefined) {
        SetMemoryEPD(sprite.act(0).dest, Add, stride);
        SetMemoryEPD(sprite.act(0).value, Add, 1);
      }

      counter.isub(1);
    };

    whileLoop(counter.ge(1), body);

    // `onPluginStart` tail: point the head/tail dwords at the blob and
    // terminate the list with the two zero pointers.
    SetMemory(start, SetTo, base);
    SetMemory(base.at(PREV_FIELD_OFFSET), SetTo, 0);
    SetMemory(end, SetTo, last);
    SetMemory(last.at(NEXT_FIELD_OFFSET), SetTo, 0);
  });
  return data;
}

/**
 * Connect the four unlimiter lists and rewrite `0x64DEBC` every loop.
 *
 * Equivalent to the plugin's `onPluginStart` plus `afterTriggerExec`. Call it
 * once at module top level, after the module's other statements, so the
 * per-loop write stays last. Returns the four backing blobs.
 */
export function unlimiter(): UnlimitedLists {
  const lists: UnlimitedLists = {
    bullets: connectDList(BULLET_LIST_ADDR, BULLET_LIST_END_ADDR, BULLET_ITEM_SIZE, BULLET_ITEM_COUNT),
    sprites: connectDList(
      SPRITE_LIST_ADDR,
      SPRITE_LIST_END_ADDR,
      SPRITE_ITEM_SIZE,
      SPRITE_ITEM_COUNT,
      1,
    ),
    images: connectDList(IMAGE_LIST_ADDR, IMAGE_LIST_END_ADDR, IMAGE_ITEM_SIZE, IMAGE_ITEM_COUNT),
    orders: connectDList(ORDER_LIST_ADDR, ORDER_LIST_END_ADDR, ORDER_ITEM_SIZE, ORDER_ITEM_COUNT),
  };
  SetMemory(PER_TICK_ADDR, SetTo, PER_TICK_VALUE);
  return lists;
}
