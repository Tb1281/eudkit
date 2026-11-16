# displayAt relative-offset and cursor restore

Status: accepted (2026-11-05; supersedes the owning-cursor experiment)

## Context

`displayAt(L)` writes text at a display line.

### History of the Bug in eudkit
1. **Original eudplib semantics (`~/eudplib`)**:
   `eudplib` implemented `DisplayAt(line)` using a relative offset and restore:
   ```python
   # eudplib/string/strbuffer.py & cpprint.py
   with FixedText(DisplayText):
       if line != 0:
           SetMemory(0x640B58, Add, line)
           if Memory(0x640B58, AtLeast, 11):
               SetMemory(0x640B58, Subtract, 11)
   # FixedText saves 0x640B58 on enter and restores 0x640B58 on exit!
   ```
   StarCraft renders circular chat lines relative to the write cursor $P$:
   $$\text{Screen Row } r = (\text{slot} - P) \pmod{11}$$
   Because `eudplib` added `line` to $P$ and restored $P$, the text written at slot $(P + \text{line}) \pmod{11}$ is rendered at:
   $$\text{Screen Row} = ((P + \text{line}) - P) \pmod{11} = \text{line}$$
   Row 0 is the top line of the chat box, and Row 10 is the bottom line.
   Subsequent plain `display()` calls write to $P$ and increment $P$, allowing new messages to scroll naturally from the bottom (Row 10) up, while `displayAt(line)` stays pinned at Row `line`.

2. **The eudkit mistake**:
   Early `eudkit` ports mistakenly changed `Add, line` to `SetTo, line`.
   Writing to absolute slot `line` while restoring $P$ caused the rendered row $((line - P) \pmod{11})$ to shift whenever other messages scrolled ($P$ advanced).

3. **The flawed owning-cursor experiment**:
   Believing the restore caused the drift, the first port removed the restore while keeping `SetTo, line`.
   Because each frame reset $P$ to 1 without restoring, $P$ never naturally advanced beyond 3 across frames:
   both `AAA` and `BBB` were frozen at the very bottom lines (Rows 9 and 10), and normal scrolling was broken.

## Decision

1. **Parity with eudplib**:
   Revert the owning semantics and restore `eudplib`'s exact relative-add and restore semantics:
   - Save current `0x640B58` (bits 8, 4, 2, 1) into `saved`.
   - If `line != 0`:
     - Add `line` to `0x640B58` (`Add, line`).
     - If `0x640B58 >= 11`: Subtract 11 (`Subtract, 11`).
   - Emit `DisplayText`.
   - Restore `0x640B58 = saved` (`SetTo, saved`).

2. **In-game Verification**:
   Verified in StarCraft: Remastered with `tests/outputmap/smoke/displayat_owning_verify.ts`:
   - `AAA` is pinned rock-solid at Line 1 (one line below the top).
   - `BBB` scrolls up smoothly from the bottom line (Row 10).

3. **Sim Contract**:
   The sim models trigger execution without StarCraft's internal synchronous `DisplayText` cursor increment.
   Because `displayAt` restores `saved`, the sim-visible pointer after `displayAt` equals the `saved` pointer value before the call.

## Consequences

- `ts/string.ts` and `py/string.py` emit the save bit-tests, relative `Add`, modulo-11 `Subtract`, `DisplayText`, and the `saved` restore.
- `ts/test_display_restore.ts`, `py/test_display_restore.py`, and the `FixedString` restore test assert transparency / restore-saved semantics.
