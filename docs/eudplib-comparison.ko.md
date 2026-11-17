# `eudkit` vs `eudplib` 비교 및 스펙 카탈로그

> **기준 시점:** `eudkit` `master` (`739657c`, `v0.1.1`) 및 `armoha/eudplib`·`armoha/euddraft` 로컬 체크아웃 기준. `eudplib` 쪽 주장은 해당 체크아웃을 기준으로 한 것이며, 이 저장소만으로는 검증할 수 없습니다.

이 문서는 `eudplib`(+ `euddraft`) 경험자가 `eudkit`을 검토하거나 마이그레이션할 때 참고할 수 있도록 **아키텍처 차이**, **전체 기능 스펙 카탈로그**, **좋아진 점**, **아쉬운 점(제약 및 미지원 기능)**을 사실 기반으로 정리한 레퍼런스입니다.

---

## 1. 한눈에 보는 아키텍처 비교

| 항목 | `eudplib` (+ `euddraft`) | `eudkit` |
|---|---|---|
| **지원 언어(DSL)** | Python, **epScript (`.eps`)** | **TypeScript**, **Python** (동일 어휘 1:1 미러링, 바이트 동일 HIR/`.scx` 출력) |
| **컴파일러 구현** | Python + C++(Lemon 파서/epScript) + Rust/C++(StormLib) | **Rust 7개 크레이트 파이프라인** (`hir` → `opt` → `lower` → `lir` → `map` → `cli`, LSP는 `eudkit-lsp`) |
| **빌드 형태** | `.edd`/`.eds` 설정 + 플러그인 러너 (`euddraft`) 또는 Python 스크립트 | 단일 CLI 바이너리 `eudkit` (`compile`, `dump-ir`, `synth`, `doctor`, `init`) |
| **중간 표현(IR) 및 최적화** | Python 객체 그래프에서 즉시 트리거/바이너리 직렬화 | **HIR 3.2(JSON)** → `eudkit-opt`(Cell 강등, 데드 게이트 폴딩, 배칭, 병합) → **LIR** |
| **변수 저장소 및 이동** | `EUDVariable`(72B 트리거 내장) + **`VProc` 자기수정(`SetNextPtr`)** | **Trigger(16C, 64A) `TriggerPage`** + `Cell` 강등 + `emitVarStore` |
| **클래식 트리거 경계** | Stage-3 인젝터가 플레이어별 리스트를 재작성해 **`RunTrigTrigger` 온디맨드 실행** | **Classic-first** (`TrigTriggerBegin`/`End`로 `curpl=0` 경계 보장, 기존 플래그 보존) |
| **1틱 1회 게이트** | `nextptr` 기반 틱/프레임 제어 | 루프 게이트(`ADR 0010`/`0013`), `--no-tick-gate`로 단일 체인 맵 옵트아웃 가능 |
| **에디터/LSP 지원** | VS Code epScript 확장(문법/일부 스니펫) | 내장 JSON-RPC LSP **`eudkit-lsp`** (VS Code Wasm 내장, Zed 자동 다운로드) |
| **빌드 재현성** | 일부 랜덤 요소(`soundlooper` 랜덤 ID 등) 존재 | **결정적 빌드** (타임스탬프·랜덤 MPQ ID를 쓰지 않음; 동일 입력 → 동일 `.scx`를 보장하는 전용 테스트는 아직 없음) |
| **내장 검증/시뮬레이터** | 런타임/단위 테스트 위주 | 컴파일 타임 `EUDSL-*` 진단 + **내장 LIR 시뮬레이터** (`eudkit-compile`) |

---

## 2. 영역별 스펙 카탈로그

> 표의 `eudkit` 이름은 **TypeScript (`camelCase`) / Python (`snake_case`)** 순으로 표기했습니다. 대부분의 코어 심볼은 양쪽 언어에서 두 철자 모두 별칭으로 export됩니다.

### 2.1 진입점 및 제어 흐름 (`temporal.ts/.py`, `conditions.ts/.py`)

| 기능 | `eudplib` / `epScript` | `eudkit` | 비고 |
|---|---|---|---|
| 모듈 진입점 | 스크립트 전역 트리거 스코프 + `onPluginStart` / `beforeTriggerExec` / `afterTriggerExec` | `buildModule(fn)` / `build_module(fn)` (`export const module` / `module = ...`) | 플러그인 훅 없이 단일 모듈 명시 ([ADR 0012](./adr/0012-explicit-entry-module-export.md)) |
| 조건 분기 | `EUDIf()` / `EUDElseIf()` / `EUDElse()` / `if (...)` | `when(conds, body, elseBody?)` (Py `with when(...):` 지원) | 런타임 조건을 Python/TS `if`에 넣으면 `EUDSL-COND-TRUTHIFY` 오류 |
| 1회/반복-해제 | `EUDExecuteOnce()` / `DoActions` 조합 | `once(conds, body)`, `until(conds, body)` | HIR 1급 노드, `opt`에서 디슈가링 ([ADR 0006](./adr/0006-hir-nodes-for-until-edge-and-counted-forms.md)) |
| 주기/지연/에지 | 직접 카운터/래치 구현 | `every(n, body)`, `after(n, body)`, `onRising` / `onFalling` | 내장 템포럴 블록 |
| 루프 | `EUDWhile()` / `EUDContinue()` / `EUDBreak()` | `whileLoop(conds, body)` / `while_loop` | 임의 `break`/`continue` 없음 (`forEachUnitList`만 `BREAK` 지원) |
| 다중 정수 분기 | `EUDSwitch(v)` (`SetNextPtr` 점프 테이블) | `dispatch(val, cases, fallback?)` | `N <= 9`는 `when` 체인, `N >= 10`은 `O(log N)` 이진 탐색 트리 ([ADR 0025](./adr/0025-multiway-dispatch.md)) |
| 논리 조건 조합 | `EUDAnd` / `EUDOr` / `EUDNot` / `&&` `\|\|` `!` | `condAll(...)`, `condAny(...)`, `condNot(...)` | De Morgan/`complement()` 정규화 + 최소 플래그 게이트 ([ADR 0028](./adr/0028-logical-condition-combinators.md)) |
| 16조건 초과 | `RawTrigger` 직접 분할 필요 | `when`/`once`/`whileLoop` 등 구조화 게이트 **자동 16C 청킹** | 원시 `Trigger`는 16C/64A 상한 유지 ([ADR 0027](./adr/0027-condition-chunking.md)) |

### 2.2 변수, 메모리 접근 및 `CurrentPlayer` (`var.ts`, `view.ts`, `cp.ts`)

| 기능 | `eudplib` | `eudkit` | 비고 |
|---|---|---|---|
| 스칼라 변수 | `EUDVariable`, `EUDLightVariable` | `Var(init)`, `lazyVar(init)` (모듈 스코프 지연 바인딩) | Py는 `+=`/`-=`/`==` 연산자 오버로딩, TS는 `.set()`/`.add()`/`.eq()` 등 메서드 |
| 상수 주소 조건 | `Memory`, `MemoryX` | `MemCond`, `MemView` 비교, `Memory`, `MemoryX` | 1C, `CurrentPlayer` 무변경 |
| 상수 주소 읽기 (`Var`) | `f_dwread_epd`, `f_wread_epd`, `f_bread_epd` (`VProc` + `CP` 변경) | `MemFieldHandle.read()`, `CUnit(epd).hp.read()` (`DeathsX` 비트 테스트) | **`CurrentPlayer`를 건드리지 않음** (공유 안전) |
| 상수 주소 쓰기 | `SetMemory`, `f_dwwrite_epd(epd, var)` | 상수: `.set(c)` / `.iadd(c)` / `SetMemory`<br>런타임 `Var`: `emitVarStore` + `Trigger.fire()` 또는 `memcpyEPD` | 필드 `.set(var)`는 `EUDSL-TYPE`으로 거부됨 |
| 동적 EPD 읽기/쓰기 | `f_dwread_epd(var_epd)`, `f_dwwrite_epd(var_epd, val)` | 읽기: `memcpyEPD(SCRATCH_EPD, varEpd, 1)` 또는 `emitVarStore` + `CPCursor(0)`<br>쓰기: 자기수정 `Trigger.fire()` 또는 `FixedArray` | `SCRATCH_EPD`는 호출자가 마련한 정적 스크래치 EPD입니다(예: `EPD(0x58A364 + 0x240)`, [examples 08](../examples/py/08_dynamic_epd_access.py) 참조). `SetMemoryEPD(var_epd, ...)`에 런타임 `Var`를 직접 넘길 수 없음 |
| CP 커서 블록 | `f_setcurpl(epd)` + `f_*_cp` + `f_setcurpl2cpcache()` | `withCP(baseEpd, cp => ...)` / `withEpd(epd, cp => ...)` | 블록 종료 시 `baseEpd`로 복원 (`0`이 아님 주의) |
| 구조화 메모리 뷰 | `CUnit`, `CSprite`, `EUDStruct` | `CUnit`, `CSprite`, `defineMemView`, `TriggerStruct` | JSON 스키마(`schema/layouts/*.json`) 정본, `CUnitView` 메서드(`giveTo`, `die`, `remove`, `setInvincible`, `isBurrowed`, `setLocation`, `setColor` 등) |
| 비트/분할 헬퍼 | `f_bitread_*`, `f_maskread_epd` | `readBit`, `readBitVar`, `writeBit`, `writeMask`, `splitDword`, `splitWord` | Rule V1(조건 내 런타임 EPD 금지) 준수 ([ADR 0039](./adr/0039-cunit-view-bit-helpers-and-object-pool.md)) |

### 2.3 스톡 트리거 & `.dat` 테이블 (`stock.*`, `dat.ts/.py`)

| 기능 | `eudplib` | `eudkit` | 비고 |
|---|---|---|---|
| 스타에딧 스톡 트리거 | 전체 조건/액션 (`RawTrigger` + `condact`) | **22개 스톡 조건 + 57개 스톡 액션** 전체 + 확장 13종 (`DeathsX`, `Memory`, `MemoryEPD`, `MemoryX`, `MemoryXEPD`, `SetDeathsX`, `SetMemory`, `SetMemoryEPD`, `SetMemoryX`, `SetMemoryXEPD`, `SetNextPtr`, `DisplayTextIndex`, `SetStringDword`) | `schema/stock-rows.json` 단일 정본 ([docs/stock-triggers.md](./stock-triggers.md)) |
| 유닛 프로퍼티(`UPRP`) | `UnitProperty` | `CreateUnitWithProperties` + `UnitProperty` | 맵 조립 시 UPRP 슬롯 자동 할당·중복 제거 |
| `.dat` 테이블 접근 | `TrgUnit`, `Weapon`, `Tech`, `Upgrade` (상수/일부 런타임) | `unitsDat`, `weaponsDat`, `flingyDat`, `techDat`, `upgradesDat` + 열거형 (`UnitType` 등) | **상수 인덱스 전용** (`Var` 전달 시 `EUDSL-TARGET`) ([ADR 0009](./adr/0009-typed-dat-accessors.md)) |

### 2.4 자료구조 & 블록 I/O (`array.ts`, `queue.ts`, `struct.ts`, `units.ts`, `blockio.ts`, `datadumper.ts`)

| 기능 | `eudplib` | `eudkit` | 비고 |
|---|---|---|---|
| 동적 인덱스 배열 | `EUDArray`, `EUDVArray` | `FixedArray(type, len)` (`PageSlotEPD` 기반), `FixedArray.const([...])` | `length >= 64` 동적 `.get()` 후 `CP`가 `0`으로 리셋됨 |
| 큐 / 덱 / 스택 | `EUDQueue`, `EUDDeque`, `EUDStack` | `Queue`, `Deque`, `Stack` (고정 용량 + `lazy*` 변형) | 정적 슬롯 트리거 바인딩 |
| 유닛 그룹 | `UnitGroup(cap)` (`cploop`, `add`, `remove`) | `UnitGroup(cap, { compact: true, autoPrune: true })` (`forEach`) | `forEach`는 `emit_cp_sweep`으로 CP 자동 저장/복원 |
| 오브젝트 풀 | `ObjPool` | `ObjectPool(maxNodes)` (`Stack` + `FixedArray`, `POOL_NULL`) | 결정적 LIFO 할당 ([ADR 0039](./adr/0039-cunit-view-bit-helpers-and-object-pool.md)) |
| 블록 복사/채우기 | `f_repmovsd_epd`, `f_memcpy` | `memcpyEPD(dst, src, count)`, `memsetEPD(dst, val, count)` | `memsetEPD` 상수 인자 시 `ceil(count/64) <= 512` 정적 `SetDeaths` 폴딩 |
| 바이너리 데이터/MPQ | `Db`, `dataDumper` (`euddraft` 플러그인), `MPQAddFile` | `Db`, `DbRef`, `epdOfDb`, `dataDumper`, `embedFile` / `embed_file` | `Db`는 트리거 페이로드 병합, `embedFile`은 MPQ 직접 임베드 |

### 2.5 문자열, HUD 출력 및 닉네임 (`string.ts/.py`, `player.ts/.py`)

| 기능 | `eudplib` | `eudkit` | 비고 |
|---|---|---|---|
| 동적 문자열 버퍼 | `StringBuffer`, `f_sprintf`, `f_eprintln`, `DisplayTextAt` | `FixedString(capacity)` (`write`, `append`, `writeFrom`, `print`, `display`, `printAt`, `displayAt`, `clear`) | 정렬된 4바이트 레인 + 용량 증명, `{}` 기본 10진수·정렬/채움·16진수 포맷 ([ADR 0003](./adr/0003-capacity-aware-aligned-string-writes.md), [ADR 0014](./adr/0014-decimal-format-default-and-fill-alignment-syntax.md)) |
| `displayAt` 커서 | `DisplayTextAt` | `displayAt` / `printAt` (`0x640B58` 상대 오프셋 + 커서 복원) | [ADR 0007](./adr/0007-displayat-owns-the-display-cursor.md) |
| 닉네임 비교/변경 | `IsPName`, `SetPName`, `SetPNamef` | `matchNames(names, onMatch, players?)`, `playerNameEpd`, `playerNameAddr` | NUL 포함 `DeathsX` 정확한 UTF-8 매칭 ([ADR 0032](./adr/0032-player-name-matching.md)); **`SetPName`/`SetPNamef`는 미지원** |

### 2.6 플레이어, 유닛 순회, 테크/업그레이드, 로케이션 (`player.ts`, `units.ts`, `scstate.ts`, `location.ts`)

| 기능 | `eudplib` | `eudkit` | 비고 |
|---|---|---|---|
| 플레이어 순회 | `EUDLoopPlayer(ptype, force, race)` (CP 미변경)<br>`EUDPlayerLoop()` (CP 변경/복원) | `forEachPlayer(body, { activeOnly?, players? })` | **`CurrentPlayer`를 건드리지 않음** (`EUDLoopPlayer` 방식); `ptype`/`force`/`race` 필터는 미내장 |
| 플레이어 존재/로컬 | `f_playerexist`, `f_getuserplayerid`, `IsUserCP`, `f_getgametick` | `playerExists`, `userPlayer`, `isUser`, `gameTick`, `turbo` | `playerExists`는 `0x51A280` 산술 프로브(CP 미변경) |
| 플레이어 상태/변수 | `scdata/player.py`, `PVariable` | `Player(0..11)` (18개 필드 `minerals`/`gas`/보급/점수), `PVariable(init?)` | `Player(p).field`는 상수 인덱스/상수 쓰기 전용 |
| 로컬 전체 출력 | `DisplayTextAll`, `PlayWAVAll`, `MinimapPingAll`, `CenterViewAll`, `SetMissionObjectivesAll`, `TalkingPortraitAll` | `displayTextAll`, `playWavAll`, `minimapPingAll`, `centerViewAll`, `setMissionObjectivesAll`, `talkingPortraitAll` | `CurrentPlayer`를 `userPlayer()`로 전환 후 복원(전환·복원 2회 저장), 루프 없음. 각 클라이언트는 자신의 `userPlayer()`로 실행하므로 옵저버도 포함됨 |
| 유닛 리스트 순회 | `EUDLoopUnit`, `EUDLoopUnit2`, `EUDLoopNewUnit`, `EUDLoopPlayerUnit` | `forEachUnitList(list, body)` (`"U"`/`"T"`/`"S"`/`"D"`, `withEpd`, `BREAK` 센티넬) | 비트맵/그룹 순회 전략 등은 미구현 ([ADR 0015](./adr/0015-unit-iteration-strategies.md)) |
| 로케이션/도형 | `f_getlocTL`, `f_setloc`, `f_addloc`, `f_dilateloc` | `Location(id)`, `getlocTl`, `drawCircle`, `drawLine`, `drawPolygon`, `drawStar` | 원시 `0..255` ID 또는 `Location` 래퍼 ([ADR 0037](./adr/0037-location-handles-and-geometry.md)) |
| 테크/업그레이드 | `Upgrade[p]`, `Tech[p]` | `techResearched`, `techResearchedVar`, `upgradeLevel`, `setTechResearched`, `setUpgradeLevel` | SC/BW 분할 테이블 자동 처리 ([ADR 0038](./adr/0038-tech-and-upgrade-state-tables.md)) |

### 2.7 수학, 난수, 카메라, 오디오 및 맵 효과 (`mathf.ts`, `random.ts`, `camera.ts`, `bgm.ts`, `soundlooper.ts`, `noaircollision.ts`, `unlimiter.ts`)

| 기능 | `eudplib` / `euddraft` | `eudkit` | 비고 |
|---|---|---|---|
| 정수 나눗셈/제곱근 | `f_div_towards_zero`, `f_div_floor`, `f_div_euclid`, `f_sqrt` | `divTowardsZero`/`modTowardsZero`, `divFloor`/`modFloor`, `divEuclid`/`modEuclid`, `isqrt`, `pow` | 복원 나눗셈 래더 공유 ([ADR 0034](./adr/0034-signed-math-trig-and-binary-search.md)) |
| 삼각함수 & 이진 탐색 | `f_atan2`, `f_atan2_256`, `f_lengthdir`, `f_lengthdir_256`, `EUDBinaryMin`/`Max` | `atan2`, `atan2_256`, `lengthdir`, `lengthdir_256`, `binaryMin`, `binaryMax` | Q16 `Db` 룩업 테이블 |
| 난수(PRNG) | `f_rand`, `f_dwrand`, `f_randomize`, `f_srand`, `f_getseed` | `rand`, `dwrand`, `randRange`, `randomize`, `srand`, `getSeed` | 빌더별 독립 LCG 상태 (`GAME_TICK_ADDR` 시딩) ([ADR 0035](./adr/0035-per-builder-lcg-prng.md)) |
| 카메라 추적 | `cammove` 플러그인 | `cameraFollow(target, opts?)` | 맵 리빌러/고정소수점 카메라 추적 |
| 오디오 (BGM / 루프) | `bgmplayer` 플러그인, `lib/soundlooper.py` | `playBgm(path, secs)`, `SoundLoops` + `SoundLooper` (`oggDuration` 내장) | 결정적 5글자 ID + `goto` 리셋 버그 수정 ([ADR 0023](./adr/0023-soundlooper-loop-player.md)) |
| 엔진 확장 효과 | `unlimiter` 플러그인, `noAirCollision` 플러그인 | `unlimiter()`, `noAirCollision()` | `buildModule` 내 직접 호출 ([ADR 0024](./adr/0024-noaircollision-scrubber-helper.md)) |

---

## 3. `eudkit`이 좋아진 점 (장점)

1. **TypeScript + Python 듀얼 프론트엔드 & IDE 경험**
   - TypeScript의 정적 타입 검사(`tsc`)와 코드 완성 위에서 맵을 작성할 수 있으며, `py/`와 `ts/`가 동일한 HIR/LIR/`.scx`를 생성하므로 팀·프로젝트 취향에 맞게 언어를 선택할 수 있습니다.
   - `eudkit-lsp`를 통해 CUnit/CSprite 필드, 스톡 트리거, 용량 진단(`EUDSL-BOUNDS`)을 VS Code와 Zed에서 쓸 수 있습니다. 릴리스 `.vsix`/Zed 자동 다운로드 기준으로는 별도 설정 없이 쓸 수 있지만, 소스에서 직접 빌드할 때는 wasm 타깃 설치가 필요합니다([VS Code](../editors/vscode/README.md), [Zed](../editors/zed/README.md)).
2. **컴파일러 파이프라인 분리와 최적화 가시성**
   - `eudkit synth` / `eudkit dump-ir --explain-folds`로 트리거·조건·액션 수(`static_cost`, `executed_cost`)와 최적화기 폴딩 내역을 맵 빌드 전 즉시 수치로 확인할 수 있습니다.
   - `eudkit-opt`가 미달성 데드 게이트 제거([ADR 0031](./adr/0031-gate-satisfiability-and-dead-body-folding.md)), 조건부 없는 `Var` 연산의 `Cell` 강등, `memsetEPD` 정적 `SetDeaths` 폴딩(최대 512 청크), 트리거 패킹(`16C/64A`)을 자동으로 수행합니다.
3. **일상적인 읽기/루프에서 `CurrentPlayer` 비오염**
   - `MemFieldHandle.read()`, `CUnit(epd).field.read()`, `playerExists`, `userPlayer`, `forEachPlayer` 등이 `0x6509B0`을 바꾸지 않으므로, 의도치 않게 `DisplayText` 대상이 바뀌거나 `CP` 복원을 깜빡해 생기는 버그가 크게 줄어듭니다.
4. **엄격한 빌드 타임 오류 검출 (`EUDSL-*`)과 자동 16조건 청킹**
   - `if (v == 1)` 같은 실수(`EUDSL-COND-TRUTHIFY`), 용량 초과/음수(`EUDSL-BOUNDS`), 스코프 밖 선언(`EUDSL-SCOPE`), 타입 불일치(`EUDSL-TYPE`)를 컴파일 즉시 진단 코드와 함께 차단합니다([docs/diagnostics.md](./diagnostics.md)).
   - `when`/`once`/`whileLoop`에 17개 이상의 조건을 넣어도 수동 분할 없이 자동으로 16C 체인으로 쪼개줍니다.
5. **결정적 빌드와 독립 단일 바이너리**
   - 타임스탬프나 랜덤 MPQ ID를 쓰지 않아 같은 소스면 동일한 `.scx`가 생성되는 결정적 빌드입니다(동일 입력 → 동일 SHA-256 `.scx` 및 정적 비용을 보장하는 전용 테스트가 아직 없어, "완전"이라는 표현은 이 문서의 주장으로만 읽어 주세요).
   - `euddraft` + Python + C++/Lemon 환경 설정 없이 `eudkit` 단일 CLI 번들(및 GitHub Releases의 휠/타볼 — PyPI/npm에는 아직 게시되지 않음)로 빌드할 수 있으며, 내장 LIR 시뮬레이터로 로워링/런타임 정합성이 검증됩니다.

---

## 4. `eudkit`이 안 좋아진 점 및 미지원 기능 (단점 / 한계)

1. **epScript(`.eps`) 미지원**
   - 기존 `.eps` 소스를 그대로 빌드할 수 없습니다. 기존 `eudplib`/`euddraft` 프로젝트를 옮기려면 TypeScript 또는 Python DSL로 재작성해야 합니다([examples/README.md](../examples/README.md) 마이그레이션 가이드 참고).
2. **`SetNextPtr` 액션은 있으나 `VProc` 패턴은 미지원**
   - **`SetNextPtr` 액션 자체는 스톡 행으로 존재**하나, `VProc`식 자기수정 변수 패턴은 지원하지 않습니다.
   - **상수 전용 쓰기/테이블 인덱싱:** `CUnit(epd).hp.set(var)`나 `unitsDat[var]`처럼 런타임 `Var`를 직접 대입하거나 인덱스로 넣을 수 없습니다(`emitVarStore` + `Trigger.fire()` 또는 `memcpyEPD` 패턴 필요).
   - **동적 EPD 읽기/쓰기 번거로움:** `eudplib`의 `f_dwread_epd(var_epd)` / `f_dwwrite_epd(var_epd, val)` 한 줄에 비해, `eudkit`에서는 `memcpyEPD(SCRATCH_EPD, varEpd, 1)` / `CPCursor(0)` / 자기수정 `Trigger.fire()` 패턴을 직접 써야 합니다(`SCRATCH_EPD`는 호출자가 마련한 정적 스크래치 EPD).
   - **`SetMemoryEPD(var_epd, ...)` 함정:** 런타임 `Var`를 `SetMemoryEPD`에 넘기면 변수가 가리키는 주소가 아니라 `var_epd` 변수의 `TriggerPage` 슬롯 자체를 수정합니다.
   - **`EUDBreak` / `EUDContinue` 부재:** 일반 `whileLoop` 안에서 조기 탈출(`break`)이나 `continue`를 쓸 수 없습니다(`forEachUnitList`만 `BREAK` 반환 지원).
3. **`euddraft` 플러그인 및 일부 고급 `eudplib` API 미구현**
   - **네트워크/입력 동기화:** `MSQC` (마우스/키보드 비공유→공유 동기화), `QueueGameCommand`, `chatEvent` (채팅 명령 파서) 없음.
   - **와이어프레임 / GRP / 닉네임 변경:** `wireframe` (`SetWireframes`, `Is64BitWireframe` 등), `eudgrp`, `SetPName` / `SetPNamef` 없음 (`matchNames` 매칭만 지원).
   - **맵 보호(Freeze / Obfuscation):** `euddraft`의 `freeze` 기능 없음 (MRGN 난독화는 페이로드 난독화일 뿐 맵 보호가 아님).
   - **플레이어 순회 필터:** `forEachPlayer`에 `EUDLoopPlayer(ptype, force, race)` 같은 맵 설정 기반 필터 인자가 없음.
   - **문자열 조립 유연성:** `StringBuffer` + `f_sprintf` 기반 동적 전역 버퍼 대신 용량 고정 `FixedString`만 지원.
   - **유닛 그룹/온디맨드 클래식 트리거:** `UnitGroup`의 `cploop` 편의 메서드나 `RunTrigTrigger` 기반 클래식 트리거 온디맨드 제어 미지원.
4. **`withCP` 종료 시 `CP` 동작 주의 및 베이스맵 필수**
   - `withCP(baseEpd, ...)`는 블록 종료 시 `0`이 아니라 `baseEpd`로 복원하므로, 직후 P1 `DisplayText`를 출력하려면 `SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)` 또는 `displayTextAll`을 써야 합니다.
   - 빈 `.scx`를 처음부터 생성하지 못하며 모든 `eudkit compile` 호출에 `--basemap`이 필요합니다.

---

## 관련 문서

- [examples/README.md](../examples/README.md) — `eudplib` → `eudkit` 마이그레이션 예제 9종 및 메모리 읽기/쓰기 결정표
- [docs/diagnostics.md](./diagnostics.md) — `EUDSL-*` 진단 코드 전체 목록
- [docs/stock-triggers.md](./stock-triggers.md) — 스톡 22C/57A 및 확장 트리거 카탈로그
- [docs/adr/README.md](./adr/README.md) — 아키텍처 결정 기록(ADR) 색인
