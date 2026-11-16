# 마이그레이션 예제 (`eudplib` → `eudkit`)

> **SSOT**: 이 문서의 영문 정본(SSOT)은 [examples/README.md](./README.md)입니다. 내용이 다를 경우 영문 정본을 우선합니다.

eudkit이 처음이라면 [`examples/quickstart/README.ko.md`](./quickstart/README.ko.md)의 점진적인 입문
예제와 [제로부터 플레이 가능한 맵까지의 안내](../docs/quickstart.ko.md)를 먼저
읽고, 네 예제가 이해된 뒤에 이 문서로 돌아오세요.

같은 메모리 읽기/쓰기, 커서, 테이블, 구조체/배열, 블록 I/O 의도를
**`eudplib`**, **`eudkit` Python**, **`eudkit` TypeScript**로 각각 어떻게
작성하는지 나란히 보여 주는 실행 가능한 예제입니다.

모든 `py/`/`ts/` 쌍은 경고 없이 합성되고 **바이트 단위로 동일한 HIR과 `.scx`
출력**을 생성하는 것으로 검증됩니다.

## 먼저 볼 곳

- 게임 안에서의 검증 절차와 예상 화면 출력은
  [`../tests/outputmap/smoke/README.md`](../tests/outputmap/smoke/README.md)를
  참고하세요.
- 이 문서는 튜토리얼이 아니라 마이그레이션 예제 모음입니다. 예제는
  `eudkit synth` / `eudkit compile`로 실행하세요.
  `python3 examples/py/01_direct_memory.py`를 직접 실행하면 이 체크아웃 대신
  PyPI의 `py` 패키지가 잡힐 수 있습니다.

## 디렉터리 구성

- `examples/eudplib/` — 고전 `eudplib` 기준 구현(`from eudplib import *`, `eudplib` 기준으로 파라미터 순서를 검증).
- `examples/py/` — `eudkit` Python DSL(`from py import ...`, `module = build_module(...)`). `pip install -e ./py`로 설치한 외부 패키지는 `from eudkit import ...`로 임포트합니다.
- `examples/ts/` — `eudkit` TypeScript DSL(`from "../../ts/index.ts"`, `export const module = buildModule(...)`). `"eudkit"` npm 소비자는 `from "eudkit"`으로 임포트합니다.

## 메모리 읽기/쓰기 선택 가이드 (Memory Read/Write Decision Matrix)

| 의도 | `eudplib` | `eudkit` (권장 API) | `CP` (`0x6509B0`) 영향 |
|---|---|---|---|
| **고정 주소 조건 비교** | `Memory`, `MemoryX`, `f_dwread_epd` 후 `EUDIf` | `MemCond`, `MemView`/`CUnit` 비교 (`1C`), `Memory`/`MemoryX` | 건드리지 않음 (안전) |
| **고정 주소 상수 쓰기** | `SetMemory`, `SetMemoryX`, `f_dwwrite_epd` | `MemFieldHandle.set(c)`, `SetMemory`/`SetMemoryXEPD` (`1A`) | 건드리지 않음 (안전) |
| **고정 주소 값 읽기 (`Var`)** | `f_dwread_epd`, `f_wread_epd`, `f_bread_epd`, `f_maskread_epd` | `MemFieldHandle(...).read()` / `CUnit(epd).field.read()` (`DeathsX` 비트 테스트) | **건드리지 않음 (`DisplayText` 안전)** |
| **연속 오프셋 다중 접근** | `f_setcurpl(epd)` + `f_*_cp` + `f_setcurpl2cpcache()` | `withCP(constEpd, cp => ...)` (`cp.at`, `cp.view(CUnit)`, 절대 `moveTo`) | **주의:** 종료 시 `baseEpd`로 복귀 (`0` 아님). 출력 전 `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)` 필요 |
| **`.dat` 테이블** | `TrgUnit(...)`, `Weapon(...)`, `Tech(...)`, `Upgrade[p]` | `unitsDat[UnitType.TerranMarine]`, `weaponsDat`, `upgradesDat`, `techDat`, `flingyDat` (상수 인덱스만 허용) | 건드리지 않음 (`MemFieldHandle` 상속) |
| **유닛 그룹 순회** | `UnitGroup(cap)` + `for u in g.cploop` | `UnitGroup(cap, { compact: true, autoPrune: true })` + `forEach` | **`emit_cp_sweep`이 기존 CP를 자동 저장/복원** |
| **활성 플레이어 순회** | `for p in EUDLoopPlayer():` | `forEachPlayer(body)` / `for_each_player(body)` | **`forEachPlayer`는 `CurrentPlayer`를 바꾸지 않음; 런타임 `0..7` 또는 언롤 `players`** |
| **로컬 UI 브로드캐스트** | `DisplayTextAll` / `PlayWAVAll` (`curpl`을 로컬 ID로 전환) | `displayTextAll` / `playWavAll` / `minimapPingAll` / `centerViewAll` / `setMissionObjectivesAll` / `talkingPortraitAll` | **`CurrentPlayer`를 `userPlayer()`(옵저버 포함)로 임시 전환 후 이전 CP 자동 복원** |
| **인덱싱 가능한 자체 배열/구조체** | `EUDArray`, `EUDVArray`, `EUDStruct` (`selftype`) | `FixedArray` (`PageSlotEPD` 64A `TriggerPage`), `FixedArray.const`, `TriggerStruct` | 동적 `FixedArray.get` (`length >= 64`)은 CP 사용 후 `0`으로 리셋 |
| **블록 복사/초기화/주입** | `f_repmovsd_epd`, `f_memcpy`, `Db`, `dataDumper` | `memcpyEPD`, `memsetEPD` (512청크 이하 정적 폴딩), `dataDumper` | `memcpyEPD`는 `CP`를 `srcEPD + count - 1`로 이동 (필요 시 `0` 복구) |
| **런타임 변수(`Var`) EPD 읽기/쓰기** | `f_dwread_epd(var_epd)`, `f_dwwrite_epd(var_epd, var_val)` | 읽기: `memcpyEPD(SCRATCH_EPD, var_epd, 1)` 또는 `emitVarStore(..., CP_ADDR)` + `CPCursor(0)`<br>쓰기: `Trigger` + `emitVarStore` + `trig.fire()` / 소유 슬롯은 `FixedArray` | `memcpyEPD` / `CPCursor(0)` 경로는 `CP = 0` 복구 필요 |

`SCRATCH_EPD`는 예제가 직접 선언하는 로컬 상수이며(예:
`examples/py/08_dynamic_epd_access.py`, `examples/ts/07_block_io_and_data.ts`)
라이브러리 export가 아닙니다. 자신의 맵에서는 아무 스크래치 dword나 골라
쓰면 됩니다. `CP_ADDR`(`0x6509B0`)은 `py`(`from py import CP_ADDR`)와
`ts/index.ts`(`import { CP_ADDR } from "../../ts/index.ts"`) 양쪽에서 다시
export됩니다.

## 주요 마이그레이션 함정 (`eudplib` → `eudkit`)

1. **`withCP`는 `CP = 0`으로 자동 복원하지 않는다 (`03_cp_cursor`)**: `CPCursor.restore()`는 블록 이탈 시 `baseEpd`로만 재고정합니다. `DisplayText` / `FixedString.displayAt`은 현재 `CurrentPlayer`에게 출력하므로 P1 출력 전 `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)`을 호출하거나, 단일 상수 EPD에는 `MemFieldHandle` / `CUnit(epd)`를 직접 쓰세요.
2. **필드 쓰기/비교 `.set(val)`은 상수만 허용한다 (`02`, `04`, `08`)**: `MemFieldHandle.set(var)`나 `u.hp = var`는 `EUDSL-TYPE` 오류를 발생시킵니다. 런타임 `Var` 값을 메모리에 쓰려면 `emitVarStore` + 자기수정 `Trigger.fire()` 또는 `memcpyEPD` / `FixedArray`를 사용하세요.
3. **값 공간(Value Space) 규칙 (`02`, `06`)**: `MemFieldHandle` / `DatFieldHandle`은 시프트된 **필드 단위(field units)**를 사용하지만(ADR 0017), `TriggerStruct`의 마스킹 필드(`byte(8)`, `bit(3, 5)`)는 **원시 레인 단위(raw lane units, shift=0)**로 비교합니다(ADR 0018). 서브워드 필드에서 `-1` 비교는 `0xFFFFFFFF`로 정규화되어 `Never()`로 폴딩되므로 `0xFF` / `0xFFFF`를 사용하세요.
4. **런타임 `Var` EPD를 `SetMemoryEPD`에 직접 넘기지 않는다 (`08`)**: `SetMemoryEPD(var_epd, ...)`는 `var_epd`가 *가리키는 주소*가 아니라 `var_epd` 변수 *자체의 `TriggerPage` 슬롯*(`mode: 1` reloc)을 가리킵니다.
5. **빌드 타임 `if`/`while` 금지 (ADR 0020)**: Python에서 `Cond`/`Var`/`MemFieldHandle`을 `if`/`while`/`assert`에 넣으면 `EUDSL-COND-TRUTHIFY`가 발생합니다. 항상 `when()`, `once()`, `whileLoop()`, `dispatch()`를 사용하세요.

## 주제 색인

| # | `eudplib` | `eudkit` Python | `eudkit` TypeScript | 초점 |
|---|---|---|---|---|
| 01 | [`01_direct_memory.py`](./eudplib/01_direct_memory.py) | [`01_direct_memory.py`](./py/01_direct_memory.py) | [`01_direct_memory.ts`](./ts/01_direct_memory.ts) | `MemCond`, `SetMemory`, CP-safe `MemFieldHandle.read()` |
| 02 | [`02_subdword_masks.py`](./eudplib/02_subdword_masks.py) | [`02_subdword_masks.py`](./py/02_subdword_masks.py) | [`02_subdword_masks.ts`](./ts/02_subdword_masks.ts) | Byte/word/bitmask (`FieldSpec`, `SetMemoryXEPD`, `TriggerStruct`) |
| 03 | [`03_cp_cursor.py`](./eudplib/03_cp_cursor.py) | [`03_cp_cursor.py`](./py/03_cp_cursor.py) | [`03_cp_cursor.ts`](./ts/03_cp_cursor.ts) | `withCP`, `CPCursor.at/view/moveTo`, `CP=0` 리셋 vs 직접 `CUnit` |
| 04 | [`04_cunit_and_groups.py`](./eudplib/04_cunit_and_groups.py) | [`04_cunit_and_groups.py`](./py/04_cunit_and_groups.py) | [`04_cunit_and_groups.ts`](./ts/04_cunit_and_groups.ts) | `CUnit`, `CSprite`, `UnitGroup(compact=True, autoPrune=True)` |
| 05 | [`05_dat_tables.py`](./eudplib/05_dat_tables.py) | [`05_dat_tables.py`](./py/05_dat_tables.py) | [`05_dat_tables.ts`](./ts/05_dat_tables.ts) | `unitsDat`, `weaponsDat`, `techDat`, `upgradesDat` |
| 06 | [`06_arrays_and_structs.py`](./eudplib/06_arrays_and_structs.py) | [`06_arrays_and_structs.py`](./py/06_arrays_and_structs.py) | [`06_arrays_and_structs.ts`](./ts/06_arrays_and_structs.ts) | `FixedArray`, `FixedArray.const`, `TriggerStruct`, `Trigger.fire()` |
| 07 | [`07_block_io_and_data.py`](./eudplib/07_block_io_and_data.py) | [`07_block_io_and_data.py`](./py/07_block_io_and_data.py) | [`07_block_io_and_data.ts`](./ts/07_block_io_and_data.ts) | `memcpyEPD`, `memsetEPD`, `Db`, `dataDumper` |
| 08 | [`08_dynamic_epd_access.py`](./eudplib/08_dynamic_epd_access.py) | [`08_dynamic_epd_access.py`](./py/08_dynamic_epd_access.py) | [`08_dynamic_epd_access.ts`](./ts/08_dynamic_epd_access.ts) | 런타임 `Var` EPD 읽기 (`memcpyEPD`/`CPCursor(0)`) & 쓰기 (`emitVarStore` + `fire()`) |
| 09 | [`09_unit_list.py`](./eudplib/09_unit_list.py) | [`09_unit_list.py`](./py/09_unit_list.py) | [`09_unit_list.ts`](./ts/09_unit_list.ts) | `for_each_unit_list`/`forEachUnitList` (`U`/`T`/`S`/`D`), `with_epd`/`withEpd`, `BREAK` 센티널 |
| 10 | [`10_players_and_broadcasts.py`](./eudplib/10_players_and_broadcasts.py) | [`10_players_and_broadcasts.py`](./py/10_players_and_broadcasts.py) | [`10_players_and_broadcasts.ts`](./ts/10_players_and_broadcasts.ts) | `for_each_player`/`forEachPlayer` (CP 유지), `Player`, `PVariable`, `player_exists`, `user_player`/`is_user`, `display_text_all`/`displayTextAll`, `match_names` |
| 11 | [`11_math_random_loc_and_tech.py`](./eudplib/11_math_random_loc_and_tech.py) | [`11_math_random_loc_and_tech.py`](./py/11_math_random_loc_and_tech.py) | [`11_math_random_loc_and_tech.ts`](./ts/11_math_random_loc_and_tech.ts) | `div_floor`, `isqrt`, `pow`, `atan2`, `lengthdir`, `binary_min`, `srand`/`randomize`/`rand_range`, `Location`/`getloc_tl`/`draw_circle`, `tech_researched`/`upgrade_level`/`set_upgrade_level` |
| 12 | [`12_containers_and_effects.py`](./eudplib/12_containers_and_effects.py) | [`12_containers_and_effects.py`](./py/12_containers_and_effects.py) | [`12_containers_and_effects.ts`](./ts/12_containers_and_effects.ts) | `Queue`, `Deque`, `Stack`, `ObjectPool`/`POOL_NULL`, `free_list_empty`, `camera_follow`/`cameraFollow`, `turbo`, `no_air_collision`, `unlimiter` |

## 모든 예제 검증

먼저 워크스페이스를 빌드한 뒤 Py/TS 동등성 검사를 실행합니다:

```sh
cargo build --workspace
PYTHONPATH=. python3 -m unittest py/tests/test_examples.py
```

`test_examples.py`는 `target/debug/eudkit` 존재를 확인하고 각 Py/TS 쌍을 합성·컴파일하며 바이트 단위로 동일한 HIR 및 `.scx` 출력을 요구합니다. 형제 `eudplib` 체크아웃이 없으면 `eudplib` 기준 구현의 임포트 검사는 건너뛰며, Py/TS 동등성 검사는 모두 실행됩니다.

예제 하나를 플레이 가능한 맵으로 컴파일하려면:

```sh
./target/debug/eudkit compile examples/ts/01_direct_memory.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/01_direct_memory.scx
```
