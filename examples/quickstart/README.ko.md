# 입문자 빠른 시작 예제

> **SSOT**: 이 문서의 영문 정본(SSOT)은 [examples/quickstart/README.md](./README.md)입니다. 내용이 다를 경우 영문 정본을 우선합니다.

작고 점진적인 **실행 가능 예제** 네 개입니다. 모든 `py/`와 `ts/` 쌍은 경고 0개로 합성되고, 바이트 단위로 동일한 compact HIR을 생성하며, 시뮬레이터의 자기 타깃 불변식을 유지하고, 바이트 단위로 동일한 `.scx` 파일을 생성하는지 검증됩니다.

eudkit이 처음이라면 먼저 [`docs/quickstart.ko.md`](../../docs/quickstart.ko.md)의 단계별 안내를 읽고 이 문서로 돌아오세요.

## 네 가지 예제

| # | Python | TypeScript | 다루는 개념 |
|---|---|---|---|
| 01 | [`py/01_hello_world.py`](./py/01_hello_world.py) | [`ts/01_hello_world.ts`](./ts/01_hello_world.ts) | `DisplayText`, `build_module` / `buildModule`, 필수 `module` 진입점 |
| 02 | [`py/02_variables_and_strings.py`](./py/02_variables_and_strings.py) | [`ts/02_variables_and_strings.ts`](./ts/02_variables_and_strings.ts) | `Var`, 런타임 연산, `FixedString` 포매팅, `display_at` / `displayAt` |
| 03 | [`py/03_control_flow_and_temporal.py`](./py/03_control_flow_and_temporal.py) | [`ts/03_control_flow_and_temporal.ts`](./ts/03_control_flow_and_temporal.ts) | `when`/`otherwise`, `once`, `every`, `after`, `dispatch`, 런타임 조건에 `if`/`while`을 쓸 수 없는 이유 |
| 04 | [`py/04_units_and_triggers.py`](./py/04_units_and_triggers.py) | [`ts/04_units_and_triggers.ts`](./ts/04_units_and_triggers.ts) | 스톡 `Trigger`/`Bring`/`CreateUnit`, `CUnit` 필드 읽기/쓰기, `FixedArray` 기초 |

각 파일 상단에는 해당 예제를 바로 실행할 수 있는 `synth` 및 `compile` 명령이 적혀 있습니다.

## 사전 요구 사항

- Rust 1.85+ (워크스페이스가 edition 2024 사용)
- Python 3.11+ (Python DSL)
- Node.js 22.6+ (TypeScript DSL 및 `ts/` 패키지 빌드)
- StarCraft: Remastered (컴파일된 `.scx`를 게임에서 열어볼 때만 필요)

저장소 루트에서 CLI를 한 번 빌드합니다:

```sh
cargo build --workspace
./target/debug/eudkit --version
```

`cargo build --workspace`는 Python 테스트 헬퍼가 HIR 시뮬레이션에 사용하는 `target/debug/eudkit-compile`도 함께 빌드합니다. 릴리스 바이너리는 `cargo build --release --workspace`로 빌드한 뒤 `target/release/eudkit`을 사용하세요.

## 예제 하나 실행하기

저장소 루트에서 실행합니다:

```sh
# Python -> compact HIR JSON
./target/debug/eudkit synth examples/quickstart/py/01_hello_world.py --compact

# TypeScript -> 바이트 단위로 동일한 compact HIR JSON
./target/debug/eudkit synth examples/quickstart/ts/01_hello_world.ts --compact

# 둘 중 하나를 플레이 가능한 맵으로 컴파일
./target/debug/eudkit compile examples/quickstart/py/01_hello_world.py \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/01_hello_world-py.scx
```

`01_hello_world` 대신 나머지 세 예제 파일 이름을 넣어도 동일하게 동작합니다.

## 빠른 시작 검사 실행

저장소 루트에서 실행합니다:

```sh
PYTHONPATH=. python3 -m unittest py/tests/test_quickstart_examples.py
```

이 테스트 스위트는 다음을 검증합니다:

1. Python과 TypeScript 예제 목록이 정확히 일치하는지
2. 두 프론트엔드 모두 **stderr 출력 없이**(경고 0개) 합성되는지
3. `--compact` HIR 출력이 바이트 단위로 동일한지
4. `run_sim`이 `self_targeted_invariant_holds: true`를 보고하는지
5. `tests/outputmap/basemap/basemap_silent.scx` 위에서 두 프론트엔드가 바이트 단위로 동일한 `.scx` 파일을 생성하는지

## 저장소 내부 임포트 vs 설치된 패키지 임포트

예제는 이 체크아웃에서 임포트합니다(`from py import ...`, `from "../../../ts/index.ts"`). 설치형 소비자는 `pip install -e ./py` 후, 또는 `cd ts && npm install && npm run build` 후 `ts/`를 설치/링크하고 `from eudkit`을 사용합니다. CLI는 체크아웃의 `py/` 및 `ts/` 트리에서 소스를 로드하며, 바이너리가 체크아웃 외부에 있다면 `EUDKIT_ROOT=/path/to/eudkit`으로 저장소를 지정하세요. 전체 표기는 [docs/quickstart.ko.md](../../docs/quickstart.ko.md#import-표기-체크아웃-vs-설치된-패키지)를 참고하세요.

예제를 `python3 examples/quickstart/py/01_hello_world.py`나 일반 `node`로 직접 실행하지 마세요. 이들은 단독 실행 스크립트가 아니라 **`eudkit` 컴파일러의 소스**입니다. 또한 저장소 루트가 `sys.path` 맨 앞에 없으면 PyPI의 동명 패키지 `py`가 저장소의 `py/` 패키지를 가릴 수 있습니다.

## 다음 단계

- [`docs/quickstart.ko.md`](../../docs/quickstart.ko.md) — `synth` → `dump-ir` → `compile` 및 게임 내 테스트를 포함한 처음부터 끝까지 입문 안내
- [`examples/README.ko.md`](../README.ko.md) — 메모리 접근, CP 커서, `CUnit` 그룹, DAT 테이블, 배열/구조체, 블록 I/O, 동적 EPD 접근 마이그레이션 예제
- [`GLOSSARY.md`](../../GLOSSARY.md) — 정본 용어 및 ADR 결정 요약 (영문)
- [`tests/outputmap/smoke/README.md`](../../tests/outputmap/smoke/README.md) — 스모크 맵에서 사용하는 게임 내 검증 절차 및 예상 출력 표 (영문)
