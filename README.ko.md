# eudkit

> **SSOT**: 이 문서의 영문 정본(SSOT)은 [README.md](README.md)입니다. 내용이 다를 경우 영문 정본을 우선합니다.

eudkit은 StarCraft: Remastered EUD 맵 제작을 위한 컴파일러 툴체인이자 통합 TypeScript/Python DSL입니다. 네이티브 `Trigger (16C, 64A)` 실행·저장 모델을 중심으로 설계되었으며, TypeScript 또는 Python으로 빌드 타임 매크로를 작성하면 트리거 레코드로 낮춘 뒤 베이스 맵 위에 오버레이해 플레이 가능한 `.scx` 파일을 생성합니다.

> **Upstream attribution:** eudkit은 [armoha/eudplib](https://github.com/armoha/eudplib) 및 [armoha/euddraft](https://github.com/armoha/euddraft)의 개념과 맵 제작 생태계를 바탕으로 개선한 독립 컴파일러·DSL 툴체인입니다.

## 4단계 파이프라인

```text
  hello.ts / hello.py
          |
          |  프론트엔드 캡처 (Node 또는 Python)
          v
  +-----------------+
  | 1. HIR          |  eudkit-hir    타입이 지정된 트리거 트리, HIR 3.2 JSON
  +-----------------+
  | 2. Optimizer    |  eudkit-opt    시간 제어 디슈가, 배칭, 병합, 스토리지 분석
  +-----------------+
  | 3. Lowering     |  eudkit-lower  스토리지 플랜, HIR -> LIR, 릴로케이션
  +-----------------+
  | 4. LIR + map    |  eudkit-lir    와이어 레코드
  |                 |  eudkit-map    CHK 픽스업, MPQ 어셈블리
  +-----------------+
          |
          v
      hello.scx
```

네 개의 컴파일러 레이어는 [ADR 0001](docs/adr/0001-trigger-16c-64a-architecture.md)에 정의된 HIR, 옵티마이저, 로워링, LIR + 맵 어셈블리입니다. Python과 TypeScript 프론트엔드는 그 위에서 HIR JSON을 CLI로 전달합니다.

컴파일은 오버레이 방식입니다. `eudkit compile`은 `--basemap <basemap.scx>`를 받아 생성된 데이터를 해당 맵의 복사본 위에 기록합니다. 저장소에는 빠른 시작과 테스트용 무음 베이스맵 `tests/outputmap/basemap/basemap_silent.scx`가 포함되어 있습니다.

## 사전 요구 사항

| 도구 | 최소 버전 | 용도 |
| --- | --- | --- |
| Rust | 1.85.0 | CLI 및 워크스페이스 크레이트 빌드 (`edition 2024`, `Cargo.toml`의 `rust-version = "1.85.0"`) |
| Node.js | 22.6 | `.ts` 소스 컴파일 (`node --experimental-strip-types` 사용) |
| Python | 3.11 | `.py` 소스 컴파일 (`py/pyproject.toml`의 `requires-python = ">=3.11"`) |

StarCraft: Remastered는 빌드나 컴파일에는 필요하지 않으며, 컴파일된 맵을 실제로 플레이할 때만 필요합니다.

## 설치

사용할 언어 프론트엔드에 맞는 패키지를 설치하세요:

```sh
pip install eudkit      # Python DSL
npm install eudkit      # TypeScript DSL
```

`eudkit` CLI와 에디터 언어 서버(`eudkit-lsp`)는 위 언어 패키지에 포함되어 있지 않습니다. [GitHub Releases](https://github.com/Tb1281/eudkit/releases/latest)에서 플랫폼에 맞는 독립 번들(`eudkit-<ver>-<target>.tar.gz`, Windows는 `.zip`)을 내려받아 압축을 풀면 `bin/eudkit`이 인접한 `share/eudkit/` 디렉터리에서 번들 DSL 런타임을 자동으로 찾습니다. 해당 플랫폼용 번들이 없다면 아래 안내에 따라 소스에서 직접 빌드하세요.

## CLI 빌드

저장소 루트에서 실행합니다:

```sh
cargo build --release --workspace
./target/release/eudkit --version
```

릴리스 바이너리는 `target/release/eudkit`입니다. 디버그 빌드는 `cargo build --workspace`로 생성된 `target/debug/eudkit`을 사용하며, Python/TypeScript 테스트 스위트도 이 디버그 바이너리를 사용합니다.

첫 빌드 시 `Cargo.toml`에 선언된 `stormlib` Git 의존성을 자동으로 내려받습니다.

## 프로젝트 설치 및 설정

릴리스 빌드는 이 체크아웃 외부에서도 동작합니다. 독립 실행형 `eudkit`은 `EUDKIT_ROOT`, 현재 또는 소스 디렉터리의 상위 체크아웃, 인접한 `share/eudkit/` 번들, 설치된 `eudkit` npm/Python 패키지, (디버그 빌드 전용) 빌드 시점 체크아웃 순으로 DSL 런타임을 찾습니다. 설정된 `EUDKIT_ROOT`가 잘못되면 오류이며 다른 런타임으로 대체하지 않습니다. 전체 탐색 순서와 `share/eudkit/` 레이아웃 계약은 [CONTRIBUTING.md#runtime-resolution](CONTRIBUTING.md#runtime-resolution)에 있습니다.

컴파일 전에 바이너리가 어떤 런타임과 프론트엔드 툴체인을 사용하는지 확인하려면:

```sh
eudkit doctor
eudkit doctor --json    # 머신 리더블 JSON 출력
```

새 디렉터리에 스타터 프로젝트를 생성하려면:

```sh
eudkit init hello-py --py
eudkit init hello-ts --ts
```

프론트엔드별로 디렉터리를 분리하세요 (`eudkit init`은 기존 스타터 파일을 덮어쓰지 않습니다). `eudkit init [<dir>]`은 `<dir>`(기본값: 현재 디렉터리)에 파일을 생성하며 `--ts` 또는 `--py`로 프론트엔드를 지정합니다.

## 60초 Hello World

저장소 루트에서 아래 명령을 실행하세요. 두 스니펫 모두 체크아웃 내부 임포트 형태를 사용하며, 패키지로 설치해 사용할 때는 `eudkit`으로 임포트합니다([py/README.ko.md](py/README.ko.md), [ts/README.ko.md](ts/README.ko.md) 참고).

### Python

`hello.py` 생성:

```python
from py import DisplayText, build_module


def prog() -> None:
    DisplayText("Hello")


module = build_module(prog)
```

컴파일:

```sh
./target/release/eudkit compile hello.py \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o hello.scx
```

### TypeScript

`hello.ts` 생성:

```ts
import { DisplayText, buildModule } from "./ts/index.ts";

export const module = buildModule(() => {
  DisplayText("Hello");
});
```

같은 형식으로 컴파일:

```sh
./target/release/eudkit compile hello.ts \
  --basemap tests/outputmap/basemap/basemap_silent.scx \
  -o hello.scx
```

StarCraft: Remastered에서 `hello.scx`를 열어 생성된 트리거를 확인할 수 있습니다. 맵을 쓰지 않고 HIR만 확인하려면 `./target/release/eudkit synth hello.py`(또는 `hello.ts`)를 실행하고, 비용 데이터까지 읽기 쉽게 보려면 `dump-ir`을 실행하세요. `--explain-folds`를 추가하면 옵티마이저의 조건/게이트 폴딩 로그가 뒤에 붙습니다.

베이스맵 선택부터 게임 내 검증까지 전체 첫 실행 흐름은 [docs/quickstart.ko.md](docs/quickstart.ko.md)와 [examples/quickstart/README.ko.md](examples/quickstart/README.ko.md)를 참고하세요.

## 저장소 구조

| 경로 | 내용 |
| --- | --- |
| `crates/eudkit-hir/` | HIR 3.2 타입, 검증, JSON |
| `crates/eudkit-opt/` | 시간 제어 디슈가링, 배칭, 병합, 스토리지 분석 |
| `crates/eudkit-lower/` | HIR → LIR 로워링 및 릴로케이션 |
| `crates/eudkit-lir/` | LIR 와이어 스키마 |
| `crates/eudkit-map/` | CHK 픽스업, MPQ 어셈블리, `.scx` 출력 |
| `crates/eudkit-cli/` | `eudkit` CLI 바이너리 (`compile`, `dump-ir`, `synth`, `doctor`, `init`) |
| `crates/eudkit-lsp/` | JSON-RPC 언어 서버 |
| `py/` | Python DSL (체크아웃 내부 임포트 `py`, 배포 패키지 `eudkit`) |
| `ts/` | TypeScript DSL (체크아웃 소스, `npm run build`로 `dist/` 생성) |
| `schema/` | 스톡 행 스펙 및 레이아웃 JSON 정본 (패키징 사본은 `py/schema/`, `ts/schema/layouts/`) |
| `examples/` | 입문용 quickstart(`examples/quickstart/`)와 `eudplib` 기준 구현 및 `py/`/`ts/` 마이그레이션 쌍 |
| `tests/` | 스톡 골든, 출력 맵 픽스처, 스모크 맵 |
| `scripts/` | 버전·번들 검사, 스톡 표면 생성, 벤치마크 및 프로브 헬퍼 |
| `docs/` | 빠른 시작, 가이드, 아키텍처 결정 기록(ADR) |

## 문서 색인

| 문서 | 다루는 내용 |
| --- | --- |
| [README.md](README.md) | 영문 메인 README (SSOT) |
| [docs/quickstart.ko.md](docs/quickstart.ko.md) | 처음부터 플레이 가능한 맵까지 단계별 가이드 ([영문 정본](docs/quickstart.md)) |
| [examples/quickstart/README.ko.md](examples/quickstart/README.ko.md) | 실행 가능한 Hello World 4단계 예제 ([영문 정본](examples/quickstart/README.md)) |
| [examples/README.ko.md](examples/README.ko.md) | `eudplib` → `eudkit` 마이그레이션 예제 및 Py/TS 동등성 ([영문 정본](examples/README.md)) |
| [py/README.ko.md](py/README.ko.md) | Python DSL 진입점, 카탈로그, DAT 테이블 ([영문 정본](py/README.md)) |
| [ts/README.ko.md](ts/README.ko.md) | TypeScript DSL 진입점, 카탈로그, DAT 테이블 ([영문 정본](ts/README.md)) |
| [editors/vscode/README.ko.md](editors/vscode/README.ko.md) | VS Code용 `eudkit-lsp` 확장 ([영문 정본](editors/vscode/README.md)) |
| [editors/zed/README.ko.md](editors/zed/README.ko.md) | Zed용 `eudkit-lsp` 확장 ([영문 정본](editors/zed/README.md)) |
| [docs/diagnostics.md](docs/diagnostics.md) | `EUDSL-*` 진단 코드 및 해결 방법 (영문) |
| [docs/lsp.md](docs/lsp.md) | `eudkit-lsp` 빌드 및 에디터 연동 (영문) |
| [docs/release.md](docs/release.md) | 릴리스 산출물, 레지스트리 시크릿, 번들 패키징 (영문) |
| [GLOSSARY.md](GLOSSARY.md) | 도메인 용어집 및 비용 단위 (영문) |
| [docs/adr/README.md](docs/adr/README.md) | 아키텍처 결정 기록(ADR) 색인 (영문) |
| [docs/stock-triggers.md](docs/stock-triggers.md) | 스톡 및 확장 트리거 카탈로그 (영문) |
| [docs/benchmarking.md](docs/benchmarking.md) | 컴파일 단계 벤치마크 하니스 (영문) |
| [tests/outputmap/smoke/README.md](tests/outputmap/smoke/README.md) | 수동 게임 내 스모크 맵 검증 절차 (영문) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 빌드/테스트 매트릭스, 스키마 동기화, 런타임 규약 (영문) |

## 빌드 및 테스트

루트 `Makefile`이 기여자 워크플로를 감싸고 있습니다: `make build`, `make test`, 그리고 `make test-quickstart`·`make test-examples` 같은 집중 타깃입니다. 전체 타깃 매트릭스, 동등한 직접 명령, 디버그 바이너리 선행 조건은 [CONTRIBUTING.md#build-and-test](CONTRIBUTING.md#build-and-test)에 있습니다.

## 버전

모든 릴리스 산출물은 하나의 통합 버전(`0.1.0`)을 공유합니다: `eudkit` CLI와 Rust 워크스페이스, LSP 에디터 확장, Python 및 npm 패키지가 모두 같습니다. 매니페스트 표, 태그 규칙, 릴리스 규약은 [docs/release.md#version-conventions](docs/release.md#version-conventions)를 참고하세요.

## 현재 상태 및 제한 사항

- 모든 `compile`에는 베이스맵(`--basemap`)이 필요하며, 빈 맵을 처음부터 생성하는 커맨드는 제공하지 않습니다.
- CLI는 체크아웃, 번들, 설치된 패키지에서 DSL 런타임을 찾습니다. 경로가 잡히지 않으면 `eudkit doctor`로 확인하세요([CONTRIBUTING.md#runtime-resolution](CONTRIBUTING.md#runtime-resolution)).
- 실제 게임 내 동작 검증은 테스트 스위트가 아닌 스모크 맵으로 수동 확인합니다.

## 감사의 글 및 라이선스

eudkit은 [armoha/eudplib](https://github.com/armoha/eudplib)과 [armoha/euddraft](https://github.com/armoha/euddraft)의 개념 및 맵 제작 생태계를 바탕으로 개선한 독립 컴파일러·DSL 툴체인입니다.

MIT. 자세한 내용은 [LICENSE](LICENSE)를 참고하세요.
