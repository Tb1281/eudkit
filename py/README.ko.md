# eudkit (Python DSL)

> **SSOT**: 이 문서의 영문 정본(SSOT)은 [py/README.md](./README.md)입니다. 내용이 다를 경우 영문 정본을 우선합니다.

eudkit DSL의 Python 구현입니다. 정본 어휘 수준에서 `ts/`와 1:1로 대응하며, 의도적으로 다르게 동작하는 일부 빌더 경계 동작은 [ADR 0026](../docs/adr/0026-builder-seam-divergence.md)에 정리되어 있습니다.

## 설치

이 패키지는 아직 PyPI에 게시되지 않았으므로, [v0.1.1 릴리스 휠](https://github.com/Tb1281/eudkit/releases/tag/v0.1.1)(`eudkit-0.1.1-py3-none-any.whl`)이나 체크아웃에서 설치하세요(아래 개발 참고). 지금은 `pip install eudkit`으로 PyPI에서 찾을 수 없습니다.

이 패키지에는 Python DSL만 포함되어 있습니다. `eudkit` 컴파일러 CLI와 `eudkit-lsp`가 필요하면 [GitHub Releases](https://github.com/Tb1281/eudkit/releases/latest)에서 플랫폼별 독립 번들을 내려받거나 저장소 루트에서 직접 빌드하세요([메인 README](../README.ko.md) 참고).

## 개발

`uv`(권장) 또는 `pip`으로 editable 설치를 진행합니다:

```sh
# `eudkit` 체크아웃을 포함한 소비자 프로젝트에서:
uv add --editable ./eudkit/py

# 또는 저장소 루트에서, 기존 virtualenv에서:
pip install -e ./py
```

먼저 Rust 워크스페이스를 빌드하세요. Python 테스트 스위트는 `target/debug/eudkit`과 `target/debug/eudkit-compile`을 실행하므로, 바이너리가 없으면 새 체크아웃에서 실패합니다:

```sh
cargo build --workspace

# 저장소 루트에서 실행:
python3 -m unittest discover -s py/tests -p "test_*.py" -t .
```

`pytest`가 아닌 `unittest`를 사용하고, `from py import ...`가 현재 체크아웃을 가리키도록 `-t .`를 유지하세요. 전체 빌드/테스트 매트릭스와 `py`/`struct` 섀도잉 설명은 [CONTRIBUTING.md#build-and-test](../CONTRIBUTING.md#build-and-test)에 있습니다.

## 패키지 구조 및 빌드

패키지 매니페스트는 `py/` 바로 아래에 있습니다([`pyproject.toml`](./pyproject.toml)):

- `packages = ["eudkit"]`
- `package-dir = {"eudkit" = "."}`
- `package-data = {"eudkit" = ["schema/layouts/*.json", "schema/stock-rows.json"]}`

DSL 프론트엔드 모듈은 `py/` 아래에 위치하며 `eudkit` 패키지 이름으로 노출됩니다. 31개 모듈은 영역별로 묶입니다:

- **코어:** `hir.py`, `var.py`, `temporal.py`, `trigger.py`, `lazy_binding.py`
- **메모리 및 뷰:** `view.py`, `backing.py`, `location.py`, `player.py`, `units.py`, `scstate.py`
- **자료 구조 및 I/O:** `array.py`, `queue.py`, `string.py`, `struct.py`, `blockio.py`, `capacity.py`
- **조건 및 수학:** `conditions.py`, `mathf.py`, `random.py`
- **DAT:** `dat.py`, `datadumper.py`
- **스톡 카탈로그:** `stock_rows_built.py`, `stock_rows_generated.py`
- **맵 효과 및 오디오:** `bgm.py`, `camera.py`, `cp.py`, `noaircollision.py`, `ogg.py`, `soundlooper.py`, `unlimiter.py`

별도의 `mem.py`는 없으며 메모리 뷰(`MemView`, `CUnit`, `CSprite`, `MemFieldHandle`, `MemCond`)는 `view.py`에 있습니다. 테스트는 `py/tests/`에 격리되어 있습니다.

패키지는 `schema/layouts/`(`MemView`가 런타임에 읽는 `CUnit`, `CSprite` 등 JSON 레이아웃)와 `schema/stock-rows.json`(`stock_rows_built.py`가 임포트 시 읽는 스톡/확장 스펙)을 모두 포함합니다.

## 사용법

### Hello World: 저장소 내부 vs 패키지 설치

체크아웃 내부에서는 패키지 디렉터리가 `py/`이므로 임포트 이름이 `py`입니다. 저장소 루트에서 컴파일러로 실행할 때(CLI가 저장소 루트를 `sys.path`에 추가) 수정 없이 그대로 동작하며, 파일을 직접 실행할 때는 `PYTHONPATH=.`이 필요합니다:

```python
# hello.py — 저장소 내부 형태 (저장소 루트에서 실행)
from py import DisplayText, build_module


def prog() -> None:
    DisplayText("Hello")


module = build_module(prog)
```

`pip install -e ./py` 등으로 설치한 뒤에는 임포트 이름이 `eudkit`이 됩니다. 컴파일러가 호출하는 `python3` 프로세스의 경로에 editable 설치가 포함되어 있을 때(예: 활성화된 virtualenv) 이 형태가 동작합니다:

```python
# hello.py — 설치된 패키지 형태 (`pip install -e ./py` 이후)
from eudkit import DisplayText, build_module


def prog() -> None:
    DisplayText("Hello")


module = build_module(prog)
```

어느 쪽이든 `./target/debug/eudkit synth hello.py`로 HIR을 생성할 수 있습니다(`cargo build --workspace` 선행). `synth`는 HIR에서 멈추고, `eudkit compile`이 그 HIR을 로워링해 `.scx`를 어셈블합니다. 임포트 이름만 다를 뿐 완전히 같은 프로그램입니다.

정본 어휘는 두 임포트 이름 아래 모두 동일하게 제공됩니다:

```python
from eudkit import (
    Var,
    Trigger,
    when,
    once,
    until,
    every,
    after,
    with_cp,
    memcpy_epd,
    memset_epd,
    CUnit,
    CSprite,
    FixedArray,
    FixedString,
    Queue,
    build_module,
)
```

TypeScript와 1:1로 공유하는 정본 어휘 외에도 Python 프론트엔드는 다음 기능을 제공합니다:

- 컨텍스트 매니저: `with when(...):`
- `Var` 연산자 오버로딩: `+=`, `-=`, `==`, `!=`, `<`, `<=`, `>`, `>=`
- 관용적 `snake_case` 별칭: `on_rising`, `on_falling`, `with_cp`, `memcpy_epd`, `memset_epd`, `while_loop`
- `FixedString` 메서드 `write_from`, `print_at`, `display_at` (`writeFrom`, `printAt`, `displayAt`의 별칭; 모듈 레벨 export는 아님)
- 뷰의 직접 프로퍼티 접근: `u.hp.set(1000)`

### 런타임 조건은 Python `bool`이 아닙니다

`when(conds, body)`는 빌드 타임에 `body`를 한 번 실행해 HIR을 조립합니다. 모든 비교(`v.eq(1)`, `v == 1`, 필드 읽기, `Never()`)는 빌드 타임 참/거짓 값이 아니라 게임 내 검사를 표현하는 HIR 레코드입니다. 여기에 Python `if`/`while`을 쓰면 `build_module` 실행 시점에 한쪽 분기만 조용히 선택되므로 `EUDSL-COND-TRUTHIFY`(ADR 0020) 오류가 발생합니다. 대신 런타임 분기 표기를 사용하세요:

| 이 코드 대신 | Python `if`/`while`의 동작 | 권장 표기 |
|---|---|---|
| `if c: A` | 빌드 타임 분기 | `when([c], A)` 또는 `with when([c]):` |
| `if c: A else: B` | `else`가 죽은 코드가 됨 | `when([c], A, B)` 또는 `with when([c]) as w: …; w.otherwise(B)` |
| 다중 정수 분기 | 수동 `when` 체인 | `dispatch(value, [(k, body), …], fallback)` |
| `while c: body` | 빌드 무한 루프 | `while_loop([c], body)` |
| `a and b` | 한 조건이 누락됨 | 둘 다 전달: `when([a, b], body)` 또는 `cond_all(a, b)` |
| `a or b` | 한 조건이 누락됨 | 하나의 `when` 안에 `cond_any(a, b)` |
| `not c`, `x if c else y`, `assert c` | 조용한 오동작 | `cond_not(c)` / 선택을 `when` 안으로 이동 |

`cond_all(...)`, `cond_any(...)`, `cond_not(...)`가 조건용 논리 조합 API입니다(camelCase 별칭도 같은 객체입니다). 같은 종류의 중첩은 플래닝 전 평탄화되며, `cond_all(x)` / `cond_any(x)`는 `x`를 반환하고, `cond_all()`은 참, `cond_any()`는 `Never()`입니다. 부정은 직접 보수(complement) 리프가 있으면 이를 쓰고, 없으면 공유 플래그 게이트 1개를 사용합니다. Raw `Trigger.conds`를 비롯해 조건이 허용되는 모든 곳에 조합할 수 있으며, 플래그가 필요한 조합기는 `build_module` 안에서 생성해야 합니다. 자세한 내용은 [ADR 0028](../docs/adr/0028-logical-condition-combinators.md)을 참고하세요.

세 번째 인자(또는 `w.otherwise(fn)` / `w.else_(fn)`)는 런타임 `else` 분기를 등록합니다. 이는 **모든 조건이 참은 아닐 때(`NOT all`)** 실행되므로 `when([a, b], A, B)`는 `a` 또는 `b` 중 하나라도 거짓이면 `B`를 실행합니다. `when(False, A, B)`는 `B`를 무조건 실행하고, `when(True, A, B)` / `when([], A, B)`는 `B`를 절대 실행하지 않습니다. `else` 분기는 `when`만 지원합니다.

`dispatch(value, [(k, body), …], fallback)`은 다중 정수 분기용 API입니다. 키는 컴파일 타임 일반 정수(32비트 정규화 및 중복 금지)이므로 탐색 구조가 빌드 타임에 고정됩니다. 런타임 값에서 분기 수 `N <= 9`이면 중첩 `when(..., else_body)` 체인으로 낮아지고, `N >= 10`이면 `O(log N)` 탐색 경로를 위해 균형 잡힌 `HirWhen` 이진 탐색 트리(내부 노드 `ge(mid)`, 리프 `eq(key)`)로 낮아집니다. 일반 정수 `value`는 빌드 타임에 폴딩되어 선택된 분기나 `fallback`만 방출되거나 아무것도 방출하지 않으며 `when` 노드를 만들지 않습니다. `fallback` 없이 미스하면 no-op입니다.

런타임 조건은 절대 Python `bool`이 아니며, `if`/`while`/`assert`에 넣으면 `EUDSL-COND-TRUTHIFY`가 발생합니다([ADR 0020](../docs/adr/0020-build-time-vs-runtime-conditions.md)).

## 진입점

`eudkit synth`, `eudkit dump-ir`, `eudkit compile`에 넘기는 소스 파일이 루트 소스입니다. 모듈 스코프의 `module` 변수에 정확히 하나의 컴파일된 모듈을 대입해야 합니다:

```python
# 저장소 내부 형태; 설치 사용자는 `from eudkit import ...` 사용.
from py import DisplayText, build_module


def prog() -> None:
    DisplayText("Hello")


module = build_module(prog)
```

`build_module`은 HIR 구성 진입점입니다. 컴파일 타임에 본문을 한 번 실행해 `CompiledModule`을 반환하며, 별도의 캡처 훅이 아닙니다. 헬퍼 함수가 자체 모듈을 만들거나 내보낼 수는 있지만 CLI는 루트 소스의 `module`만 읽습니다([ADR 0012](../docs/adr/0012-explicit-entry-module-export.md)).

## 스톡 트리거 카탈로그

DSL은 StarEdit 스톡 조건 22종과 스톡 액션 57종 전체에 대한 헬퍼를 내보냅니다. 각 헬퍼는 문서화된 CHK 조건/액션 타입을 방출하며 스톡 파라미터 순서와 기본 플래그를 따릅니다:

```python
# 저장소 내부 형태; 설치 사용자는 `from eudkit import ...` 사용.
from py import (
    Anywhere,
    AtLeast,
    Bring,
    CreateUnit,
    P1,
    Trigger,
    UnitType,
    build_module,
)


def prog() -> None:
    Trigger(
        conds=[Bring(P1, AtLeast, 1, UnitType.TerranMarine, Anywhere)],
        acts=[CreateUnit(1, UnitType.TerranMarine, Anywhere, P1)],
        preserved=True,
    )


module = build_module(prog)
```

`CreateUnitWithProperties`는 20바이트 UPRP 레코드를 만드는 `UnitProperty` 빌더와 짝을 이루며, 슬롯은 맵 어셈블리 단계에서 할당·중복 제거됩니다. `Transmission`은 텍스트를 `strid`로, 사운드 이름(HIR `wav`)을 `wavid`로 해석하며 `PlayWAV`는 HIR `wav`만 사용합니다. 스톡 카탈로그는 StarEdit 표준 어휘이며, `DeathsX`, `Memory`, `SetDeathsX`, `SetMemoryX` 같은 마스킹 메모리 헬퍼는 스톡 와이어 형식을 재사용하지만 StarEdit 표준 행이 아니므로 **eudkit 확장(extension)** 으로 분류됩니다. 스톡 헬퍼는 상수를 받지만, `Deaths`와 `SetDeaths`는 `player` 확장 타깃과 `amount`/`value`에 대해 문서화된 동적 형태도 허용합니다.

전체 지원 매트릭스, 파라미터 순서, 기본 플래그, 스톡/확장 구분은 [`docs/stock-triggers.md`](../docs/stock-triggers.md)를 참고하세요.

Python은 임포트 시 `py/stock_rows_built.py`에서 `schema/stock-rows.json`을 읽어 특수 행이 아닌 모든 스톡/확장 헬퍼를 동적으로 생성합니다. 체크인되는 생성 산출물은 정렬된 export 이름 목록인 `py/stock_rows_generated.py`이며, 스펙을 수정한 뒤에는 `python3 scripts/generate_stock_surfaces.py`로 재생성하세요(`--check` 모드는 드리프트 시 실패). TypeScript는 `ts/stock.generated.ts`를 체크인하고 Python은 임포트 시 본문을 생성해 이름 목록만 체크인하는 비대칭은 ADR 0009/0016에 기록되어 있습니다. 특수 행 5종(`Deaths`, `SetDeaths`, `CreateUnitWithProperties`, `SetStringDword`, `UnitProperty`)만 수동 작성으로 유지됩니다.

## DAT 테이블

`dat-*.json` 스키마가 네이티브 데이터 테이블용 타입 접근자를 구동합니다:

```python
# 저장소 내부 형태; 설치 사용자는 `from eudkit import ...` 사용.
from py import UnitType, WeaponType, build_module, units_dat, weapons_dat


def prog() -> None:
    units_dat[UnitType.Marine].maxHp.set(40)
    units_dat[UnitType.Marine].max_hp.iadd(10)
    weapons_dat[WeaponType.GaussRifle].damage.set(6)


module = build_module(prog)
```

엔트리는 상수 인덱스만 허용합니다(`table.at(i)` / `table[i]`). `Var`를 넘기면 `EUDSL-TARGET`, 범위 초과 인덱스나 알 수 없는 필드는 `EUDSL-BOUNDS` 오류가 발생합니다. 필드 이름은 스키마 표기(`maxHp`), `snake_case`(`max_hp`), 또는 스키마 별칭을 모두 허용합니다.

## 입문자를 위한 핵심 개념

- **`Var`** — 빌드 타임 초깃값을 가진 가변 스칼라 핸들입니다. 옵티마이저가 `TriggerPage`에 배치하거나 dword/bit 셀로 내립니다. `v2 = v1`은 같은 슬롯을 가리키는 별칭이며, 독립 복사는 `Var(v1)` 또는 `v1.copy()`를 사용하세요. 즉시형 `Var`는 `build_module` 내부에서 만들고, `lazyVar`는 모듈 스코프에 선언한 뒤 첫 사용 시 빌더에 바인딩합니다.
- **`FixedString` + `DisplayText`** — `FixedString(size)`는 STR 기반 고정 크기 텍스트 버퍼입니다(`write`, `append`, `writeFrom`, `print`, `display`, 채팅 줄 메서드 `printAt`/`displayAt` 및 `snake_case` 별칭 제공). `DisplayText(text)`는 모든 플레이어가 아니라 `CurrentPlayer`(`0x6509B0`)에게 `text`를 표시하는 스톡 액션을 방출하며, 두 번째 인자 `always_display`의 기본값은 `4`입니다.
- **`CUnit`** — 유닛 EPD 위에서 동작하는 제로-스토리지 `MemView`입니다(`u = CUnit(epd)` 후 `u.hp.set(1000)` 또는 `u.hp.read()`). 필드 쓰기와 비교는 필드 단위를 사용하며(ADR 0017), 읽기는 `CurrentPlayer`를 건드리지 않습니다.
- **`FixedArray`** — 페이지 정렬된 `TriggerPage` 슬롯으로 뒷받침되는 고정 용량 배열입니다. 상수 인덱스는 순수 주소 계산이며 동적 인덱스는 캐리 없는 `PageSlotEPD` 가중치를 사용합니다. `FixedArray.const([...])`는 페이지를 쓰지 않는 빌드 타임 테이블입니다.
- **시간 제어 블록** — `when(conds, body)`는 조건이 성립하는 매 루프마다 실행되고, `once`는 한 번만 실행 후 비활성화되며, `until`은 조건이 거짓인 동안 실행됩니다. `every(n, body)`와 `after(n, body)`는 카운터 기반이고, `on_rising` / `on_falling`은 에지 트리거, `while_loop`는 프레임 내 구조적 반복문, `dispatch`는 다중 정수 분기입니다.

## 참고 문서

- [빠른 시작 (한국어)](../docs/quickstart.ko.md) / [Quickstart (영문 SSOT)](../docs/quickstart.md)
- [입문자 빠른 시작 예제 (한국어)](../examples/quickstart/README.ko.md) / [영문 SSOT](../examples/quickstart/README.md)
- [마이그레이션 예제 (한국어)](../examples/README.ko.md) / [영문 SSOT](../examples/README.md)
- [진단 코드 레퍼런스 (영문)](../docs/diagnostics.md)
- [언어 서버 (`eudkit-lsp`, 영문)](../docs/lsp.md)
- [용어집 (영문)](../GLOSSARY.md)
- [게임 내 스모크 맵 검증 (영문)](../tests/outputmap/smoke/README.md)
