# VS Code용 eudkit LSP

> **SSOT**: 이 문서의 영문 정본(SSOT)은 [editors/vscode/README.md](./README.md)입니다. 내용이 다를 경우 영문 정본을 우선합니다.

[eudkit](https://github.com/Tb1281/eudkit) TypeScript 및 Python DSL 파일에 대한 자동 완성, 호버 문서, 고정 용량 진단을 제공합니다. 이 확장은 WebAssembly로 컴파일된 `eudkit-lsp` (`eudkit-lsp.wasm`)를 내장하고 있어 별도의 바이너리를 내려받지 않아도 확장 설치만으로 바로 동작하며, 설정 또는 로컬 빌드가 있으면 네이티브 `eudkit-lsp` 바이너리를 우선 사용합니다.

## 설치

1. VS Code Marketplace 또는 Open VSX에서 확장을 설치합니다:

   ```sh
   code --install-extension Tb1281.eudkit-lsp
   ```

   호환 에디터용 Open VSX에도 동일한 확장 ID(`Tb1281.eudkit-lsp`)로 게시됩니다. 대신 GitHub 릴리스 빌드를 설치하려면 릴리스 페이지에서 `eudkit-vscode.vsix`를 내려받아 실행하세요:

   ```sh
   code --install-extension eudkit-vscode.vsix
   ```

2. (선택) 체크아웃에서 직접 패키징하려면(`eudkit-lsp.wasm` 빌드를 위해 `rustup target add wasm32-wasip1` 필요):

   ```sh
   cd editors/vscode
   npm install
   npm run build:wasm
   npx --yes @vscode/vsce@4.1.1 package --out eudkit-vscode.vsix
   code --install-extension eudkit-vscode.vsix
   ```

   또는 체크아웃에서 개발 모드로 바로 실행할 수 있습니다:

   ```sh
   code --extensionDevelopmentPath="$PWD/editors/vscode" .
   ```

## 서버 찾기

확장은 다음 순서로 `eudkit-lsp`를 찾습니다:

1. `eudkit.lsp.path` 설정(비어 있지 않은 경우). 신뢰되지 않은 워크스페이스(Untrusted Workspace)에서는 사용자 수준 설정만 적용되며 워크스페이스 설정은 무시됩니다.
2. 워크스페이스 폴더 내부의 `target/release/eudkit-lsp` 또는 `target/debug/eudkit-lsp`(Windows는 `.exe`). 신뢰된 워크스페이스에서만 동작합니다.
3. `PATH` 상의 `eudkit-lsp`.
4. 확장에 내장된 `eudkit-lsp.wasm`(VS Code 확장 호스트의 Node WASI 런타임으로 실행).

서버가 워크스페이스 밖에 있다면 경로를 명시적으로 지정하세요:

```json
{
  "eudkit.lsp.path": "/absolute/path/to/eudkit/target/release/eudkit-lsp"
}
```

설정을 바꾸면 클라이언트가 즉시 재시작됩니다. 서버를 시작할 수 없으면 모달 오류 창을 띄우는 대신 **eudkit LSP** 출력 채널에 시도한 명령을 한 번 기록하고 해당 채널을 열어 주므로, eudkit이 없는 일반 TypeScript/Python 워크스페이스를 방해하지 않습니다.

신뢰되지 않은 워크스페이스에서는 제한적 지원만 제공됩니다: 전역으로 설정된 서버나 `PATH`의 서버는 사용할 수 있지만, 워크스페이스 내부 바이너리 탐색과 워크스페이스 설정 재정의는 워크스페이스를 신뢰하기 전까지 비활성화됩니다.

## 언어 기능

이 확장은 얇은 클라이언트입니다. 자동 완성, 호버 문서, `EUDSL-BOUNDS` 용량 진단은 `eudkit-lsp`가 구현합니다. 파일은 언어 기준으로 매칭되며 `file` 스킴의 TypeScript 및 Python 문서에 연결됩니다. 전체 기능 목록, 진단, 문서화된 제한 사항은 [docs/lsp.md](../../docs/lsp.md#supported-features)를 참고하세요.

## 개발

```sh
npm install
npm run build:wasm
npm test
npx --yes @vscode/vsce@4.1.1 package --out eudkit-vscode.vsix
```

`npm test`는 `node --test`로 `vscode` 스텁 기반 단위 테스트를 실행합니다. `vsce`가 `vscode-languageclient`를 `.vsix`에 번들링할 수 있도록 `node_modules`는 의도적으로 패키징 제외 대상에 넣지 않았습니다.

## 라이선스

MIT. 자세한 내용은 [LICENSE](./LICENSE)를 참고하세요.
