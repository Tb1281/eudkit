# Zed용 eudkit LSP

> **SSOT**: 이 문서의 영문 정본(SSOT)은 [editors/zed/README.md](./README.md)입니다. 내용이 다를 경우 영문 정본을 우선합니다.

eudkit TypeScript 및 Python DSL용 언어 서버 [`eudkit-lsp`](../../crates/eudkit-lsp)를 실행하는 Zed 확장입니다. 자동 완성, 호버 문서, 고정 용량 진단을 제공하며, 스크립트를 컴파일하거나 맵 바이트를 변경하지 않습니다.

## 사전 요구 사항

- Zed 확장이 사용하는 WebAssembly 타깃이 포함된 Rust 툴체인:

  ```sh
  rustup target add wasm32-wasip2
  ```

- `eudkit-lsp` 바이너리. 저장소 루트에서 빌드합니다:

  ```sh
  cargo build -p eudkit-lsp --release
  ```

  생성된 `target/release/eudkit-lsp`를 `PATH`에 넣거나 아래 안내처럼 Zed 설정에 절대 경로를 지정하세요.

## 개발 확장으로 설치

1. Zed에서 Extensions 페이지를 엽니다.
2. **Install Dev Extension**을 선택합니다.
3. 이 저장소의 `editors/zed` 디렉터리를 선택합니다.
4. 확장이나 설정을 변경한 뒤에는 언어 서버를 재시작하거나 프로젝트를 다시 여세요.

Zed는 `editors/zed`를 `wasm32-wasip2` 타깃의 독립 Cargo 패키지로 컴파일하며, 이 패키지는 저장소 루트 워크스페이스에 포함되지 않습니다.

## Zed 설정

`extension.toml`이 이미 `eudkit-lsp`를 `TypeScript`와 `Python` 모두에 바인딩하고 있으므로 별도의 `language_servers` 항목은 필요하지 않습니다. 기존 언어 서버와의 실행 순서를 명시적으로 지정하고 싶을 때만 추가하세요(서버 ID: `eudkit-lsp`):

```json
{
  "languages": {
    "TypeScript": {
      "language_servers": ["eudkit-lsp", "..."]
    },
    "Python": {
      "language_servers": ["eudkit-lsp", "..."]
    }
  }
}
```

`"..."` 자리에 기존 서버 이름(예: `"typescript-language-server"`, `"pyright"`)을 넣거나 `eudkit-lsp`만 쓰려면 제거하세요. 이 블록이 없어도 Zed는 기본 순서를 유지한 채 `eudkit-lsp`를 실행합니다.

`eudkit-lsp`가 `PATH`에 없다면 절대 경로, 인자, 환경 변수를 명시적으로 설정하세요:

```json
{
  "lsp": {
    "eudkit-lsp": {
      "binary": {
        "path": "/absolute/path/to/eudkit/target/release/eudkit-lsp",
        "arguments": [],
        "env": {}
      }
    }
  }
}
```

`arguments`와 `env`는 선택 사항이며, `binary.path`를 생략하면 `which("eudkit-lsp")`로 폴백합니다. 둘 다 찾지 못하면 Zed가 바이너리 설치 또는 `lsp.eudkit-lsp.binary.path` 설정을 안내하는 오류를 표시합니다.

## 동작 방식

이 확장은 `eudkit-lsp`를 실행하며, 서버가 전체 문서 동기화 위에서 자동 완성, 호버 문서, 고정 용량 진단을 제공합니다. 전체 기능 목록, 진단, 문서화된 제한 사항과 VS Code·Neovim 설정은 [`docs/lsp.md`](../../docs/lsp.md#supported-features)를 참고하세요.

## 라이선스

MIT. 자세한 내용은 [LICENSE](./LICENSE)를 참고하세요.
