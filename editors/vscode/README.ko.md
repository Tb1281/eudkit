# VS Code용 eudkit LSP

> **SSOT**: 이 문서의 영문 정본(SSOT)은 [editors/vscode/README.md](./README.md)입니다. 내용이 다를 경우 영문 정본을 우선합니다.

[eudkit](https://github.com/Tb1281/eudkit) TypeScript 및 Python DSL 파일에 대한 자동 완성, 호버 문서, 고정 용량 진단을 제공합니다. 이 확장은 eudkit 저장소의 `eudkit-lsp` 언어 서버에 연결하는 얇은 클라이언트이며 자체 DSL 로직은 포함하지 않습니다.

## 설치

1. eudkit 체크아웃에서 언어 서버를 빌드합니다:

   ```sh
   cargo build -p eudkit-lsp --release
   ```

   `target/release/eudkit-lsp`(Windows는 `eudkit-lsp.exe`)가 생성됩니다. `cargo build -p eudkit-lsp`로 만든 디버그 바이너리도 동작합니다.

2. VS Code Marketplace 또는 Open VSX에서 확장을 설치합니다:

   ```sh
   code --install-extension Tb1281.eudkit-lsp
   ```

   호환 에디터용 Open VSX에도 동일한 확장 ID(`Tb1281.eudkit-lsp`)로 게시됩니다. 대신 GitHub 릴리스 빌드를 설치하려면 릴리스 페이지에서 `eudkit-vscode.vsix`를 내려받아 실행하세요:

   ```sh
   code --install-extension eudkit-vscode.vsix
   ```

   체크아웃에서 직접 패키징하려면:

   ```sh
   cd editors/vscode
   npm install
   npx @vscode/vsce package --out eudkit-vscode.vsix
   code --install-extension eudkit-vscode.vsix
   ```

   또는 체크아웃에서 개발 모드로 바로 실행할 수 있습니다:

   ```sh
   code --extensionDevelopmentPath="$PWD/editors/vscode" .
   ```

## 서버 바이너리 찾기

확장은 다음 순서로 `eudkit-lsp` 바이너리를 찾습니다:

1. `eudkit.lsp.path` 설정(비어 있지 않은 경우). 신뢰되지 않은 워크스페이스(Untrusted Workspace)에서는 사용자 수준 설정만 적용되며 워크스페이스 설정은 무시됩니다.
2. 워크스페이스 폴더 내부의 `target/release/eudkit-lsp` 또는 `target/debug/eudkit-lsp`(Windows는 `.exe`). 신뢰된 워크스페이스에서만 동작합니다.
3. `PATH` 상의 `eudkit-lsp`.

서버가 워크스페이스 밖에 있다면 경로를 명시적으로 지정하세요:

```json
{
  "eudkit.lsp.path": "/absolute/path/to/eudkit/target/release/eudkit-lsp"
}
```

설정을 바꾸면 클라이언트가 즉시 재시작됩니다. 서버를 시작할 수 없으면 모달 오류 창을 띄우는 대신 **eudkit LSP** 출력 채널에 시도한 명령을 한 번 기록하고 해당 채널을 열어 주므로, eudkit이 없는 일반 TypeScript/Python 워크스페이스를 방해하지 않습니다.

신뢰되지 않은 워크스페이스에서는 제한적 지원만 제공됩니다: 전역으로 설정된 서버나 `PATH`의 서버는 사용할 수 있지만, 워크스페이스 내부 바이너리 탐색과 워크스페이스 설정 재정의는 워크스페이스를 신뢰하기 전까지 비활성화됩니다.

## 기능

- DSL 어휘(스톡 및 확장 헬퍼, 핵심 식별자, Python 표기)와 `.` 뒤의 수신자 멤버(`Trigger`, `ActView`, `CondView`, `CUnit`, `CSprite`, `FixedString`, `FixedArray`)에 대한 **자동 완성(Completion)**.
- 어휘 식별자 및 멤버에 대한 **호버(Hover)** 문서 (Markdown).
- 소스 `eudkit-lsp`, 코드 `EUDSL-BOUNDS`의 **용량 진단(Diagnostics)**: `FixedString` 용량은 `1..=65535`, `FixedArray` / `Queue` / `Deque` / `Stack` / `UnitGroup` 용량은 양수여야 하며, `Trigger`는 최대 `conds` 16개, `acts` 64개까지만 담을 수 있습니다. 문자열 리터럴과 주석은 검사하지 않습니다.

파일은 언어 기준으로 매칭되며 `file` 스킴의 TypeScript 및 Python 문서에 연결됩니다.

## 제한 사항

- **타입 추론 없음.** 수신자 타입은 식별자 이름(`u`/`unit` → `CUnit`, `s`/`sprite` → `CSprite`, `trig`/`trigger`/`t` → `Trigger`, `str`/`msg` → `FixedString`, `arr`/`array` → `FixedArray`)으로 추정하므로, 이름이 `s`인 `FixedString`에는 `CSprite` 멤버가 제안됩니다.
- **용량 검사만 수행.** `EUDSL-TYPE`, `EUDSL-SCOPE` 등 나머지 진단 코드는 LSP가 아니라 eudkit 프론트엔드와 컴파일러가 생성합니다.
- **컴파일 미수행.** 빌드나 로워링을 실행하지 않으며 컴파일된 맵 바이트에도 영향을 주지 않습니다.
- **전체 문서 동기화만 지원.** 증분 편집(incremental edit)은 지원하지 않으며 디스크가 아니라 에디터 버퍼 내용을 기준으로 동작합니다.
- **정의 이동, 참조 찾기, 이름 변경, 포매팅, 코드 액션, 시그니처 도움말, 시맨틱 토큰, 워크스페이스 심볼 미지원.**
- **Python 규칙은 `languageId` 기반.** `python` 문서에만 Python 주석 및 삼중 따옴표 문자열 처리가 적용됩니다.

## 개발

```sh
npm install
npm test
npx --yes @vscode/vsce@4.1.1 package --out eudkit-vscode.vsix
```

`npm test`는 `node --test`로 `vscode` 스텁 기반 단위 테스트를 실행합니다. `vsce`가 `vscode-languageclient`를 `.vsix`에 번들링할 수 있도록 `node_modules`는 의도적으로 패키징 제외 대상에 넣지 않았습니다.

## 라이선스

MIT. 자세한 내용은 [LICENSE](./LICENSE)를 참고하세요.
