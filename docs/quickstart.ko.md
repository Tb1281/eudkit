# 빠른 시작: 처음부터 플레이 가능한 `.scx`까지

> **SSOT**: 이 문서의 영문 정본(SSOT)은 [docs/quickstart.md](./quickstart.md)입니다. 내용이 다를 경우 영문 정본을 우선합니다.

이 문서는 새로 체크아웃한 저장소에서 시작해 StarCraft: Remastered에서 바로
열 수 있는 맵을 만드는 과정을 안내합니다. [`examples/quickstart/README.ko.md`](../examples/quickstart/README.ko.md)에
있는 점진적인 예제 네 개를 사용합니다.

용어가 낯설다면 정본 정의가 모여 있는 [`GLOSSARY.md`](../GLOSSARY.md)를 먼저
펼쳐 보세요. 짧게 요약하면 이렇습니다. eudkit은 Python 또는 TypeScript
프로그램을 StarCraft 맵 페이로드로 컴파일합니다. 소스 코드에는 트리거, 게임
메모리, HUD 텍스트를 기술하고, `eudkit compile`이 그 페이로드를 베이스 맵
(`--basemap`)과 결합해 `.scx` 파일을 만듭니다.

## 0. 준비물

| 도구 | 필요한 이유 | 참고 |
|---|---|---|
| Rust 1.85+ | `eudkit` CLI 빌드 | 워크스페이스가 edition 2024를 사용합니다 |
| Python 3.11+ | Python DSL, 테스트 | 저장소 안 패키지 이름은 `py`입니다 |
| Node.js 22.6+ | TypeScript DSL | 프런트엔드가 Node의 타입 스트리핑을 호출합니다 |
| StarCraft: Remastered | 게임 안에서 검증 | synth/compile만 할 때는 선택 사항입니다 |

**베이스맵(basemap)** 은 지형, 플레이어, 설정을 그대로 재사용하는 평범한
`.scx` 맵입니다. 이 저장소에는 테스트와 예제용 베이스맵이 하나 들어 있습니다.

```text
tests/outputmap/basemap/basemap_silent.scx
```

`basemap_silent.scx`는 `basemap.scx`에서 원래 작성된 클래식 트리거를 비활성화한
맵입니다. 따라서 HUD에 보이는 줄은 여러분이 컴파일한 프로그램이 출력한 것뿐입니다.
반대로 작성된 `Hello World!` 트리거를 먼저 실행하고 싶다면 `basemap.scx`를
사용하세요.

## 1. CLI 빌드

저장소 루트에서 실행합니다.

```sh
cargo build --workspace
./target/debug/eudkit --version
```

워크스페이스는 `py/tests/helpers.py`가 사용하는 네이티브 시뮬레이터 래퍼
`target/debug/eudkit-compile`도 함께 빌드합니다. 더 빠른 릴리스 바이너리가
필요하다면 다음과 같이 빌드하세요.

```sh
cargo build --release --workspace
./target/release/eudkit --version
```

아래 명령들은 `./target/debug/eudkit`을 사용합니다. 릴리스로 빌드했다면
`./target/release/eudkit`으로 바꿔 쓰면 됩니다.

> CLI가 DSL 런타임을 찾을 수 있어야 합니다. `EUDKIT_ROOT`, 현재 또는 소스 디렉터리의 상위 체크아웃, 인접한 `<prefix>/share/eudkit/` 번들, 설치된 `eudkit` npm/Python 패키지, (디버그 빌드 전용) 빌드 시점 체크아웃 순으로 런타임을 확인합니다. 설정된 `EUDKIT_ROOT`가 잘못되면 오류이며 다른 런타임으로 대체하지 않습니다. 전체 순서와 `share/eudkit/` 레이아웃 계약은 [CONTRIBUTING.md#runtime-resolution](../CONTRIBUTING.md#runtime-resolution)에 있습니다.

### `doctor`로 런타임 확인

`eudkit doctor`는 바이너리가 사용할 DSL 런타임 루트와 프런트엔드 도구 체인을
보고합니다. 현재 디렉터리와 환경을 검사하며, `compile`과 `synth`는 소스 파일의
상위 디렉터리도 추가로 확인합니다. 스크립트, 버그 리포트, CI 로그에는 `--json`을
사용하세요.

```sh
./target/debug/eudkit doctor
./target/debug/eudkit doctor --json
```

보고된 루트가 잘못되었다면 `EUDKIT_ROOT=/path/to/eudkit`를 설정하세요.

### `init`으로 프로젝트 만들기

`eudkit init [<dir>] [--ts|--py]`는 `<dir>`(기본값: 현재 디렉터리)에 스타터
프로젝트를 만듭니다. 배우는 동안에는 프런트엔드를 명시하고, `init`이 기존
스타터를 덮어쓰지 않으므로 프런트엔드마다 별도 디렉터리를 사용하세요.

```sh
./target/debug/eudkit init hello-py --py
./target/debug/eudkit init hello-ts --ts
```

생성된 모듈을 고친 뒤 아래의 `synth` 또는 `compile`을 실행하면 됩니다.

## 2. 가장 작은 프로그램에서 시작하기

Python 소스는
[`examples/quickstart/py/01_hello_world.py`](../examples/quickstart/py/01_hello_world.py)입니다.

```python
from py import DisplayText, build_module, once


def prog() -> None:
    once([], lambda: DisplayText("Hello from eudkit!"))


module = build_module(prog)
```

TypeScript 소스는
[`examples/quickstart/ts/01_hello_world.ts`](../examples/quickstart/ts/01_hello_world.ts)입니다.

```ts
import { DisplayText, buildModule, once } from "../../../ts/index.ts";

export const module = buildModule(() => {
  once([], () => {
    DisplayText("Hello from eudkit!");
  });
});
```

`once([], ...)`는 첫 번째 트리거 루프에서만 해당 줄을 표시합니다. 이것을
빼면 `DisplayText`가 매 루프마다 같은 채팅 줄을 덧붙입니다.

`eudkit`에 전달하는 모든 소스는 `module`이라는 이름의 컴파일된 모듈을 정확히
하나 노출해야 합니다.

- Python: 모듈 스코프에 `module = build_module(prog)`를 둡니다.
- TypeScript: `export const module = buildModule(...)`를 사용합니다.

`build_module` / `buildModule`은 본문을 **컴파일 타임에 한 번** 실행하고 HIR
(중간 표현)을 기록합니다. 런타임 캡처 훅이 아닙니다. 자세한 내용은
[ADR 0012](adr/0012-explicit-entry-module-export.md)를 참고하세요.

### import 표기: 체크아웃 vs 설치된 패키지

예제는 이 체크아웃을 기준으로 작성되어 있습니다. CLI가 저장소의 `py/`와
`ts/` 트리를 직접 읽기 때문입니다.

| 모드 | Python | TypeScript |
|---|---|---|
| 이 저장소 안 | `from py import ...` | `from "../../../ts/index.ts"` |
| 설치된 라이브러리 | `from eudkit import ...` | `from "eudkit"` |

설치된 Python 패키지 표기를 쓰려면 다음을 실행합니다.

```sh
pip install -e ./py
```

설치된 TypeScript 표기를 쓰려면 먼저 패키지를 빌드한 뒤 `ts/`를 설치하거나
링크합니다.

```sh
cd ts
npm install
npm run build
npm link          # 또는: npm install /path/to/eudkit/ts
cd ..
```

이제 소비자 프로젝트에서 `from "eudkit"`이라고 쓸 수 있습니다. import 이름만
바뀌고 모듈 본문은 그대로입니다. CLI는 위에서 설명한 순서로 체크아웃 또는 설치된
패키지를 런타임으로 선택합니다. 둘 다 있으면 체크아웃을 우선하므로, 이 저장소의
DSL 소스를 캡처하려면 `EUDKIT_ROOT`가 이 저장소를 가리키도록 유지하세요.

## 3. `synth`: 소스 → HIR

`synth`는 프런트엔드를 실행해 HIR JSON을 출력합니다. 한 줄짜리, diff에 친화적인
형태가 필요하면 `--compact`를 사용하세요.

```sh
./target/debug/eudkit synth examples/quickstart/py/01_hello_world.py --compact
```

예상되는 compact 출력은 다음과 같습니다.

```json
{"version":"3.2","module":{"stmts":[{"kind":"once","conds":[],"body":[{"kind":"act","mask":0,"strid":0,"wavid":0,"time":0,"dest":0,"value":0,"unit":0,"acttype":9,"modifier":0,"flags":4,"padding":0,"eudx":0,"text":"Hello from eudkit!"}]}]}}
```

결과를 파일로 저장해 두면 나중에 들여다보거나 재사용하기 좋습니다.

```sh
./target/debug/eudkit synth examples/quickstart/py/01_hello_world.py \
  -o /tmp/quickstart-hello.hir.json
```

두 프런트엔드는 하나의 HIR 파이프라인을 공유하므로 TypeScript 예제도 같은
바이트를 만들어냅니다.

```sh
./target/debug/eudkit synth examples/quickstart/ts/01_hello_world.ts --compact
```

## 4. `dump-ir`: 비용과 저장소 구조 확인

`dump-ir`는 정적 비용과 시뮬레이션 비용, 저장소 계획, 트리거 패킹 결과를
보고합니다. 소스 파일과 `.hir.json` 파일을 모두 받습니다.

```sh
# 소스에서 바로, 트리거 루프 두 번 시뮬레이션
./target/debug/eudkit dump-ir examples/quickstart/py/01_hello_world.py \
  --json --loops 2

# 또는 위에서 저장한 HIR에서, 실행 없이
./target/debug/eudkit dump-ir /tmp/quickstart-hello.hir.json \
  --json --no-exec
```

유용한 필드는 다음과 같습니다.

| 필드 | 의미 |
|---|---|
| `static_cost` | 페이로드에 들어가는 트리거, 조건, 액션, 바이트 수 |
| `executed_cost` | 시뮬레이션한 트리거 루프 두 번에서 실제로 방문한 비용 |
| `storage` | `Var` 셀, `TriggerPage` 슬롯/페이지, dword/bit 셀 |
| `packing` | 조건과 액션이 트리거 안에 어떻게 패킹되었는지 |

`--explain-folds`를 추가하면 옵티마이저의 폴딩 로그가 뒤에 붙습니다: 조건 융합, 상수 게이트 열기, 죽은 게이트 제거, 형제 `when` 병합, 16개 조건 청킹입니다.

```sh
./target/debug/eudkit dump-ir /tmp/quickstart-hello.hir.json --explain-folds
```

텍스트 보고서는 `Folds:` 블록으로 끝납니다. `--json`을 쓰면 같은 이벤트가 `folds` 키 아래(`kind`, `site`, `before`, `after`)에 나타납니다. 이 키는 플래그가 있을 때만 포함되므로 기본 JSON 출력은 바뀌지 않습니다.

시뮬레이터는 생성된 트리거의 모델을 실행할 뿐, StarCraft 자체는 아닙니다.
게임을 열기 전에 절대 실행되지 않는 분기나 값이 변하지 않는 `Var`를 잡아내는
가장 빠른 방법입니다.

## 5. `compile`: HIR → 플레이 가능한 `.scx`

Python 예제를 silent 베이스맵 위에 컴파일합니다.

```sh
./target/debug/eudkit compile examples/quickstart/py/01_hello_world.py \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/01_hello_world-py.scx
```

TypeScript 쌍둥이 예제도 컴파일합니다.

```sh
./target/debug/eudkit compile examples/quickstart/ts/01_hello_world.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o /tmp/01_hello_world-ts.scx
```

두 파일은 바이트 단위로 동일합니다. 출력의 마지막 줄에는 정적 트리거 비용이
다음처럼 표시됩니다.

```text
compiled examples/quickstart/py/01_hello_world.py -> /tmp/01_hello_world-py.scx (36T static)
```

`T`는 트리거를 뜻합니다. 맵을 쓰지 않고 정적 비용만 확인하려면
`eudkit dump-ir <source> --json --no-exec`를 사용하세요(§4 참고).
`--check-cost`는 호환성을 위해 받아들이는 더 이상 쓰이지 않는 no-op이고,
`--plan-out <plan.json>`은 나중에 베이스맵 지문을 검증하는 재사용 가능한 빌드
계획을 기록합니다.

`-o`를 생략하면 CLI가 소스 옆에 출력 파일 이름을 정합니다. 배우는 동안에는
소스 옆 산출물을 실수로 덮어쓰지 않도록 `-o`를 명시하는 편이 좋습니다.

## 6. StarCraft에서 열기

1. 컴파일한 `.scx`를 StarCraft: Remastered가 읽는 맵 폴더로 복사합니다. 정확한
   디렉터리는 플랫폼과 설치 방식에 따라 다릅니다. 게임의 **Maps** 브라우저에
   보이는 폴더라면 어디든 됩니다.
2. StarCraft: Remastered를 실행하고 **Single Player → Custom Game**에서 해당
   맵을 고릅니다.
3. 게임을 시작합니다. hello-world 맵은 다음 줄을 보여 줍니다.

   ```text
   Hello from eudkit!
   ```

   이 줄은 로컬 플레이어(솔로 게임에서는 P1)의 채팅/HUD 줄로 표시됩니다.

저장소에 확립된 게임 내 검증 절차는
[`tests/outputmap/smoke/README.md`](../tests/outputmap/smoke/README.md)에 있습니다.
예제를 더 추가할 때 이 문서를 활용하세요.

- 커밋된 각 스모크 맵이 표시해야 하는 정확한 HUD 줄을 나열합니다.
- `DisplayText`와 `displayAt`의 차이처럼 줄 순서에 영향을 주는 요소를
  설명합니다.
- 커밋된 `.scx` 산출물을 동일한
  `--basemap tests/outputmap/basemap/basemap_silent.scx` 명령으로 다시 생성하는
  방법을 보여 줍니다.

디버깅할 때 유용한 첫 번째 규칙은 이것입니다. `synth`는 성공하는데 맵에 아무것도
보이지 않는다면, 맵을 **싱글 플레이어**로 시작했는지, 그리고 `displayAt`이
가리키는 줄을 보고 있는지 확인하세요. `DisplayText`는 줄을 덧붙이고,
`displayAt(n)`은 채팅 줄 `n`(0–10)에 씁니다. 둘 다 `CurrentPlayer`
(`0x6509B0`)를 대상으로 하며, 일반적인 솔로 시작에서는 페이로드 러너가 이 값을
0(P1)으로 둡니다.

## 7. 네 단계로 언어 익히기

각 예제를 위와 같이 `synth`와 `compile`로 실행한 뒤 주석을 읽어 보세요.

| 단계 | 예제 | 배우는 내용 |
|---|---|---|
| 01 | [Hello World](../examples/quickstart/py/01_hello_world.py) | 필수 `module` 엔트리와 `DisplayText` |
| 02 | [Variables and Strings](../examples/quickstart/py/02_variables_and_strings.py) | 게임 시간 `Var` 연산과 `FixedString`/`display_at` |
| 03 | [Control Flow and Time](../examples/quickstart/py/03_control_flow_and_temporal.py) | `when`/`otherwise`, `once`, `every`, `after`, `dispatch` |
| 04 | [Units and Triggers](../examples/quickstart/py/04_units_and_triggers.py) | 스톡 트리거, `CUnit` 필드, `FixedArray` |

### Python/TypeScript 개발자를 놀라게 하는 한 가지 규칙

런타임 조건은 **언어의 불리언이 아닙니다**.

```python
phase = Var(0)
# phase.eq(0)은 게임 메모리를 기술합니다. True/False가 아니라 HIR 조건입니다.
when([phase.eq(0)], do_something)   # 올바름: 런타임 분기를 방출합니다
# if phase.eq(0): ...               # 잘못됨: EUDSL-COND-TRUTHIFY
```

`if`/`while`은 게임이 존재하기 전, 빌드 타임에 실행됩니다. 런타임 결정에는
`when`/`once`/`while_loop`, `every`, `after`, `dispatch`를 사용하세요.
[ADR 0020](adr/0020-build-time-vs-runtime-conditions.md)과
[`py/README.md`](../py/README.md), [`ts/README.md`](../ts/README.md)의 런타임
truthiness 표를 참고하세요.

## 8. 자동 검사 실행

저장소 루트에서 실행합니다.

```sh
# 네 개의 quickstart 쌍: slug 일치, 경고 0개, HIR 동등성,
# 시뮬레이터 불변식, .scx 동등성.
PYTHONPATH=. python3 -m unittest py/tests/test_quickstart_examples.py

# 더 오래된 마이그레이션 예제도 계속 통과해야 합니다.
PYTHONPATH=. python3 -m unittest py/tests/test_examples.py
```

quickstart 스위트는 `target/debug/eudkit`과 `target/debug/eudkit-compile`을 모두
필요로 합니다. `cargo build --workspace`가 둘 다 만들어 줍니다.

루트 `Makefile`은 두 스위트를 다음과 같이 감싸고 있습니다.

```sh
make test-quickstart
make test-examples
```

이 저장소에서는 `pytest`가 아니라 `python3 -m unittest`를 사용하세요. 저장소
루트에서 `py`라는 이름의 로컬 패키지가 pytest 자체의 `py` 의존성과 충돌합니다.

## 9. 다음 단계

quickstart 예제 네 개가 이해되었다면 다음 순서로 진행하세요.

1. [`examples/README.md`](../examples/README.md)를 읽고 마이그레이션 예제를
   순서대로 풀어 보세요. 직접 메모리 접근, sub-dword 마스크, CP 커서, `CUnit`
   그룹, DAT 테이블, 배열/구조체, 블록 I/O, 동적 EPD 접근을 다룹니다. 한국어
   안내는 [`examples/README.ko.md`](../examples/README.ko.md)에서 볼 수 있습니다.
2. 정본 용어를 확인할 수 있도록 [`GLOSSARY.md`](../GLOSSARY.md)를 곁에 두세요.
3. 설계 선택이 의외로 보이면 [`docs/adr/README.md`](adr/README.md)의 결정 기록을
   읽어 보세요.
4. 전체 API 표면과 스톡 트리거 카탈로그는 [`py/README.ko.md`](../py/README.ko.md)와
   [`ts/README.ko.md`](../ts/README.ko.md)에서 확인하세요([영문 py/README.md](../py/README.md), [영문 ts/README.md](../ts/README.md)).
5. 스모크 맵과 예상 줄 표를
   [`tests/outputmap/smoke/README.md`](../tests/outputmap/smoke/README.md)에서
   참고해 여러분만의 게임 내 테스트를 만드세요.
6. 오류 코드와 수정 방법은 [`docs/diagnostics.md`](./diagnostics.md)에서, 편집기
   연동은 [`docs/lsp.md`](./lsp.md)에서 확인하세요.

## 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| `ModuleNotFoundError: No module named 'eudkit'` | 체크아웃 안 소스는 `from py import ...`를 씁니다. `pip install -e ./py` 이후에만 `eudkit`을 사용하세요. |
| `ERR_MODULE_NOT_FOUND: Cannot find package 'eudkit'` | 체크아웃 안 소스는 `"../../../ts/index.ts"`를 import합니다. 설치형 소비자는 `cd ts && npm run build` 후 설치/링크가 필요합니다. |
| 파일을 직접 실행할 때 `ImportError: cannot import name ... from 'py'` | `python3 file.py`가 아니라 `./target/debug/eudkit`으로 소스를 실행하세요. PyPI 패키지 `py`가 저장소 패키지를 가릴 수 있습니다. |
| `EUDSL-COND-TRUTHIFY` | `if`/`while`/`assert`에서 런타임 조건을 사용했습니다. `when`, `once`, `while_loop`, `every`, `after`, `dispatch` 중 하나를 쓰세요. |
| `EUDSL-TS-TRUTHIFY` | TypeScript 런타임 조건을 JS 불리언처럼 사용했습니다. 옵트인 lint가 켜져 있을 때 발생하니 `when`/`once` 같은 시간 표현으로 바꾸세요. |
| 테스트에서 `missing compiled binary: .../target/debug/eudkit` | 먼저 `cargo build --workspace`를 실행하세요. |
| `synth` 중 stderr에 프런트엔드 경고 | 소스가 권장하지 않는 관용구를 씁니다. `--no-warnings`를 붙이지 말고 소스를 고치세요. quickstart 테스트는 빈 stderr를 요구합니다. |
| `compile: cannot infer frontend` / DSL을 찾을 수 없음 | 체크아웃에서 실행하거나 `EUDKIT_ROOT`를 체크아웃 루트로 설정하세요. |
| 맵은 컴파일되지만 작성된 `Hello World!` 줄이 보임 | `basemap.scx`를 사용했습니다. `basemap_silent.scx`를 쓰거나 작성된 트리거를 감안하세요. |
