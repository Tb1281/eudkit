# eudkit (TypeScript DSL)

> **SSOT**: 이 문서의 영문 정본(SSOT)은 [ts/README.md](./README.md)입니다. 내용이 다를 경우 영문 정본을 우선합니다.

eudkit DSL의 TypeScript 구현입니다. `py/`와 정본 어휘를 공유하며, 일부 빌더 경계 동작은 의도적으로 다릅니다. 자세한 내용은 [ADR 0026](../docs/adr/0026-builder-seam-divergence.md)을 참고하세요.

## 설치

이 패키지는 아직 npm에 게시되지 않았으므로, [v0.1.1 릴리스 타볼](https://github.com/Tb1281/eudkit/releases/tag/v0.1.1)(`eudkit-0.1.1.tgz`)이나 체크아웃에서 설치하세요(아래 참고). 지금은 `npm install eudkit`으로 npm에서 찾을 수 없습니다.

이 패키지에는 컴파일된 TypeScript DSL(`dist/`), 레이아웃 스키마, MIT 라이선스만 포함되어 있습니다. `eudkit` 컴파일러 CLI와 `eudkit-lsp`가 필요하면 [GitHub Releases](https://github.com/Tb1281/eudkit/releases/latest)에서 플랫폼별 독립 번들을 내려받거나 저장소 루트에서 직접 빌드하세요([메인 README](../README.ko.md) 참고). 체크아웃에서 이 패키지를 빌드하려면 아래 `npm run build`가 필요합니다.

## 개발

먼저 Rust 워크스페이스를 빌드하세요. 테스트 하니스는 `target/debug/eudkit`과 `target/debug/eudkit-compile`을 실행하므로, 바이너리가 없으면 새 체크아웃에서 `npm test`가 실패합니다. 전체 빌드/테스트 매트릭스는 [CONTRIBUTING.md#build-and-test](../CONTRIBUTING.md#build-and-test)에 있습니다.

```sh
# 저장소 루트에서:
cargo build --workspace

# ts/ 디렉터리에서:
npm install        # 개발 도구 전용 (typescript, @types/node; 배포물에 포함되지 않음)
npm run typecheck  # 소스 + 테스트 전체에 tsc --noEmit 및 truthiness 검사 실행
npm run dev        # 로컬 개발 중 연속 재빌드 (tsc --watch)
npm test           # node --test tests/test_*.ts 실행
```

## 빌드 (JS + `.d.ts`)

`ts/`에서 실행합니다(루트 `package.json`은 없습니다):

```sh
npm run build      # 소스에서 dist/ 클린 재빌드
```

빌드는 번들러 없이 순수 `tsc`(`tsconfig.build.json`)를 사용합니다. `rewriteRelativeImportExtensions`가 소스의 `.ts` 임포트 지정자를 출력 JS의 `.js`로 변환하며, 모든 모듈 옆에 `.d.ts`를 함께 생성합니다. 테스트(`test_*.ts`)와 픽스처(`*_fixture.ts`)는 빌드에서 제외됩니다.

`dist/`는 빌드 출력 디렉터리이므로 gitignore 처리되어 있으며 커밋되지 않습니다. 새 체크아웃에서는 `ts/`에서 `npm install && npm run build`를 실행해 빌드합니다. 빌드는 타입 체크와 함께 진행되므로 타입 오류가 있으면 선언 파일이 어긋난 패키지를 내보내지 않고 즉시 실패합니다. `npm run typecheck`는 파일 방출 없이 테스트까지 포함해 같은 검사를 수행합니다.

## 사용법

### Hello World: 저장소 내부 vs 패키지 설치

체크아웃 내부에서는 패키지 디렉터리가 `ts/`이며 컴파일러는 항상 원본 `.ts` 소스를 직접 로드합니다. 저장소 루트에서 두 단계 아래에 있는 파일(`examples/ts/` 등)은 명시적 경로로 임포트하면 수정 없이 `eudkit synth`로 실행됩니다:

```ts
// hello.ts — 저장소 내부 형태 (examples/ts/hello.ts 위치 기준)
import { DisplayText, buildModule } from "../../ts/index.ts";

export const module = buildModule(() => {
  DisplayText("Hello");
});
```

`ts/`에서 `npm run build`를 실행한 뒤에는 패키지 이름으로 임포트할 수 있습니다. 외부 npm 소비자가 사용하는 형태입니다:

```ts
// hello.ts — 설치된 패키지 형태 (ts/에서 `npm run build` 이후)
import { DisplayText, buildModule } from "eudkit";

export const module = buildModule(() => {
  DisplayText("Hello");
});
```

컴파일러는 `--experimental-strip-types` 플래그로 Node를 호출하므로 **Node >= 22.6**이 필요합니다(플래그는 Node 22.6.0에 추가됨). 소스 파일을 직접 실행할 때도 같은 플래그가 필요합니다: `node --experimental-strip-types hello.ts`.

`package.json`은 다음 빌드 출력을 가리킵니다:

- `main` / `exports` → `./dist/index.js` (별도 TS 로더 없이 일반 Node로 실행 가능)
- `types` → `./dist/index.d.ts`

패키지는 `dist/`와 `schema/layouts/`(`MemView` 스키마가 런타임에 읽는 레이아웃 JSON)를 배포하며 원본 `.ts` 소스는 포함하지 않습니다. `dist/`에서는 `view.ts`의 `dist/../schema/layouts` 폴백으로 스키마를 찾으므로 `dist/schema`를 중복 복사할 필요가 없습니다.

## 진입점

`eudkit synth`, `eudkit dump-ir`, `eudkit compile`에 넘기는 소스 파일이 루트 소스입니다. `module`이라는 이름으로 컴파일된 모듈 하나를 반드시 export해야 합니다:

```ts
// examples/ts/hello.ts 기준 저장소 내부 형태.
// 설치된 패키지를 쓰는 경우 `npm run build` 후 "eudkit"에서 임포트.
import { DisplayText, buildModule } from "../../ts/index.ts";

export const module = buildModule(() => {
  DisplayText("Hello");
});
```

`buildModule`은 HIR 구성 진입점입니다. 컴파일 타임에 본문을 한 번 실행해 `CompiledModule`을 반환하며, 캡처 훅이 아닙니다. 헬퍼가 자체 모듈을 만들거나 export할 수는 있지만 CLI는 루트 소스의 `module`만 읽습니다([ADR 0012](../docs/adr/0012-explicit-entry-module-export.md)).

## 런타임 조건 및 핵심 개념

`when(conds, body)`는 빌드 타임에 본문을 한 번 조립하고 게임 내에서 조건이 성립하는 동안 실행합니다. 비교와 필드 읽기(`v.eq(1)`, `u.hp`, `Always()`, `Never()`)는 JavaScript 불리언이 아니라 HIR 레코드입니다. 여기에 일반 `if`/`while`을 쓰면 빌드 타임에 객체 참/거짓만 테스트하고 맙니다. `tsc`가 `if (v == 1)`은 TS2367로 잡아주지만, 잡지 못하는 패턴(`if (v.eq(1))`, `while (u.hp)`, `.initialValue` 비교)은 옵트인 AST 검사가 탐지합니다. `npm run typecheck`(내부적으로 `typecheck:truthiness` 실행)를 돌리면 `EUDSL-TS-TRUTHIFY`를 보고하며, `eudkit synth` 중에는 실행되지 않습니다. 대신 런타임 분기 표기를 사용하세요:

| 이 코드 대신 | JS `if`/`while`의 동작 | 권장 표기 |
|---|---|---|
| `if (c) A` | 빌드 타임 분기 | `when([c], A)` 또는 `when(c, A)` |
| `if (c) A else B` | `else`가 죽은 코드가 됨 | `when([c], A, B)` |
| 다중 정수 분기 | 수동 `when` 체인 | `dispatch(value, [[k, body], …], fallback)` |
| `while (c) body` | 빌드 무한 루프 | `whileLoop([c], body)` |
| `a && b` | 한 조건이 누락됨 | 둘 다 전달: `when([a, b], body)` 또는 `condAll(a, b)` |
| `a \|\| b` | 한 조건이 누락됨 | 하나의 `when` 안에 `condAny(a, b)` |
| `!c`, `c ? x : y`, `assert(c)` | 조용한 오동작 | `condNot(c)` / 선택을 `when` 안으로 이동 |

`condAll(...)`, `condAny(...)`, `condNot(...)`가 조건용 논리 조합 API입니다. 같은 종류의 중첩은 플래닝 전 평탄화되며, `condAll(x)` / `condAny(x)`는 `x`를 반환하고, `condAll()`은 참, `condAny()`는 `Never()`입니다. 부정은 직접 보수(complement) 리프가 있으면 이를 쓰고, 없으면 공유 플래그 게이트 1개를 사용합니다. Raw `Trigger.conds`를 비롯해 조건이 허용되는 모든 곳에 조합할 수 있으며, 플래그가 필요한 조합기는 `buildModule` 안에서 생성해야 합니다. 자세한 내용은 [ADR 0028](../docs/adr/0028-logical-condition-combinators.md)을 참고하세요.

핵심 런타임 어휘는 `py/`와 1:1로 대응합니다:

- **시간 제어 블록** — `when(conds, body)`는 조건이 성립하는 매 루프마다 실행되고, `once`는 한 번만 실행 후 비활성화되며, `until`은 조건이 거짓인 동안 실행됩니다. `every(n, body)`와 `after(n, body)`는 카운터 기반이고, `onRising` / `onFalling`은 에지 트리거, `whileLoop`는 프레임 내 구조적 반복문, `dispatch`는 다중 정수 분기입니다.
- **`Var`** — 빌드 타임 초깃값을 가진 가변 스칼라 핸들입니다. 옵티마이저가 `TriggerPage`에 배치하거나 dword/bit 셀로 내립니다. `const v2 = v1`은 같은 슬롯의 별칭이며, 독립 복사는 `Var(v1)` 또는 `v1.copy()`를 사용하세요. 즉시형 `Var`는 `buildModule` 내부에서 생성하고, `lazyVar`는 모듈 스코프에 선언한 뒤 첫 사용 시 빌더에 바인딩합니다.
- **`FixedString` + `DisplayText`** — `FixedString(size)`는 STR 기반 고정 크기 텍스트 버퍼입니다(`write`, `append`, `writeFrom`, `print`, `display`, 채팅 줄 메서드 `printAt`/`displayAt`). `DisplayText(text)`는 모든 플레이어가 아니라 `CurrentPlayer`(`0x6509B0`)에게 `text`를 표시하는 스톡 액션을 방출하며, 두 번째 인자 `alwaysDisplay`의 기본값은 `4`입니다.
- **`CUnit`** — 유닛 EPD 위에서 동작하는 제로-스토리지 `MemView`입니다(`const u = CUnit(epd)` 후 `u.hp.set(1000)` 또는 `u.hp.read()`). 필드 쓰기와 비교는 필드 단위를 사용하며(ADR 0017), 읽기는 `CurrentPlayer`를 건드리지 않습니다.
- **`FixedArray`** — 페이지 정렬된 `TriggerPage` 슬롯으로 뒷받침되는 고정 용량 배열입니다. 상수 인덱스는 순수 주소 계산이며 동적 인덱스는 캐리 없는 `PageSlotEPD` 가중치를 사용합니다. `FixedArray.const([...])`는 페이지를 쓰지 않는 빌드 타임 테이블입니다.

## 스톡 트리거 카탈로그

DSL은 StarEdit 스톡 조건 22종과 스톡 액션 57종 전체에 대한 헬퍼를 export합니다. 각 헬퍼는 문서화된 CHK 조건/액션 타입을 방출하며 스톡 파라미터 순서와 기본 플래그를 따릅니다:

```ts
// examples/ts/stock.ts 위치 기준 저장소 내부 형태.
// 설치된 패키지는 `npm run build` 후 "eudkit"에서 임포트.
import {
  Anywhere, AtLeast, Bring, CreateUnit, P1, Trigger, UnitType, buildModule,
} from "../../ts/index.ts";

export const module = buildModule(() => {
  new Trigger({
    conds: [Bring(P1, AtLeast, 1, UnitType.TerranMarine, Anywhere)],
    acts: [CreateUnit(1, UnitType.TerranMarine, Anywhere, P1)],
    preserved: true,
  });
});
```

`CreateUnitWithProperties`는 20바이트 UPRP 레코드를 만드는 `UnitProperty` 빌더와 짝을 이루며, 슬롯은 맵 어셈블리 단계에서 할당·중복 제거됩니다. `Transmission`은 텍스트를 `strid`로, 사운드 이름(HIR `wav`)을 `wavid`로 해석하며 `PlayWAV`는 HIR `wav`만 사용합니다. 스톡 카탈로그는 StarEdit 표준 어휘이며, `DeathsX`, `Memory`, `SetDeathsX`, `SetMemoryX` 같은 마스킹 메모리 헬퍼는 스톡 와이어 형식을 재사용하지만 StarEdit 표준 행이 아니므로 **eudkit 확장(extension)** 으로 분류됩니다. 스톡 헬퍼는 상수를 받지만, `Deaths`와 `SetDeaths`는 `player` 확장 타깃과 `amount`/`value`에 대해 문서화된 동적 형태도 허용합니다.

전체 지원 매트릭스, 파라미터 순서, 기본 플래그, 스톡/확장 구분은 [`docs/stock-triggers.md`](../docs/stock-triggers.md)를 참고하세요.

스톡 및 확장 헬퍼 본문은 `schema/stock-rows.json`에서 생성되어 `ts/stock.generated.ts`에 저장됩니다. 스펙을 수정한 뒤에는 `npm run generate:stock`을 실행하세요(드리프트 시 freshness 테스트가 실패함). 특수 행 5종(`Deaths`, `SetDeaths`, `CreateUnitWithProperties`, `SetStringDword`, `UnitProperty`)만 수동 작성으로 유지됩니다.

## DAT 테이블

`dat-*.json` 스키마가 네이티브 데이터 테이블용 타입 접근자를 생성합니다:

```ts
// examples/ts/dat.ts 위치 기준 저장소 내부 형태.
// 설치된 패키지는 `npm run build` 후 "eudkit"에서 임포트.
import {
  UnitType, WeaponType, buildModule, unitsDat, weaponsDat,
} from "../../ts/index.ts";

export const module = buildModule(() => {
  unitsDat[UnitType.Marine].maxHp.set(40);
  unitsDat[UnitType.Marine].maxHp.iadd(10);
  weaponsDat[WeaponType.GaussRifle].damage.set(6);
});
```

엔트리는 상수 인덱스만 허용합니다(`table.at(i)` / `table[i]`). `Var`를 넘기면 `EUDSL-TARGET`, 범위 초과 인덱스나 알 수 없는 필드는 `EUDSL-BOUNDS` 오류가 발생합니다. 필드 이름은 스키마 표기, `snake_case`(`max_hp`), 또는 스키마 별칭을 모두 허용합니다. `ts/dat.generated.ts`는 체크인되어 있으며 `schema/layouts/dat-*.json` 필드를 수정한 뒤에는 `npm run generate:dat`을 실행하세요.

## 참고 문서

- [빠른 시작 (한국어)](../docs/quickstart.ko.md) / [Quickstart (영문 SSOT)](../docs/quickstart.md)
- [입문자 빠른 시작 예제 (한국어)](../examples/quickstart/README.ko.md) / [영문 SSOT](../examples/quickstart/README.md)
- [마이그레이션 예제 (한국어)](../examples/README.ko.md) / [영문 SSOT](../examples/README.md)
- [진단 코드 레퍼런스 (영문)](../docs/diagnostics.md)
- [언어 서버 (`eudkit-lsp`, 영문)](../docs/lsp.md)
- [용어집 (영문)](../GLOSSARY.md)
- [게임 내 스모크 맵 검증 (영문)](../tests/outputmap/smoke/README.md)
