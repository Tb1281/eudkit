"""11. Math, random, locations, and tech/upgrades — eudkit Python DSL.

Migration Guide (eudplib -> eudkit):
  [EN]
  - eudplib: math (`f_div_floor`, `f_sqrt`, `f_pow`, `f_atan2`, `f_lengthdir`,
    `EUDBinaryMin`), LCG RNG (`f_srand`, `f_randomize`, `f_dwrand`), locations
    (`f_setloc`, `f_getlocTL`), and `TrgPlayer.tech`/`upgrade` are spread
    across `eudlib` and `scdata`.
  - eudkit: unified core exports:
    - `div_floor` (plus `div_towards_zero`/`div_euclid`), `isqrt`, `pow`,
      `atan2`/`lengthdir` (backed by lazy shared trig tables per builder),
      and `binary_min`/`binary_max`.
    - `srand`, `randomize`, `rand`, `dwrand`, `rand_range` with builder-local
      LCG state (no global seed collisions across modules).
    - `Location(0)` OOP wrapper (`set_rect`, `move`, `center`, `top_left`),
      `getloc_tl`, and compile-time shape drawers (`draw_circle`, `draw_line`,
      `draw_polygon`, `draw_star`).
    - `tech_researched`, `set_tech_researched`, `upgrade_level`,
      `set_upgrade_level` over SC/BW split byte tables (`0..43` / `0..60`).

  [KO]
  - eudplib에서는 수학(`f_sqrt`, `f_atan2`, `EUDBinaryMin`), 난수(`f_srand`,
    `f_randomize`), 로케이션(`f_setloc`, `f_getlocTL`), 테크/업그레이드가
    여러 서브패키지에 나뉘어 있습니다.
  - eudkit은 루트 모듈에서 직접 제공합니다:
    - `div_floor`/`isqrt`/`pow`/`atan2`/`lengthdir`/`binary_min`: 빌더별
      공유 삼각함수 테이블을 자동 방출합니다.
    - `srand`/`randomize`/`rand_range`: 빌더 격리형 LCG 상태를 사용합니다.
    - `Location(0)` 래퍼와 `getloc_tl`, 빌드 타임 도형 생성기(`draw_circle` 등).
    - `tech_researched`/`set_tech_researched`/`upgrade_level`/
      `set_upgrade_level`로 SC/BW 분할 바이트 테이블을 직접 읽고 씁니다.

External package note: when installed via ``pip install -e ./py``, import
symbols from ``eudkit`` instead of ``py`` (``from eudkit import ...``).
"""

from py import (
    Location,
    UnitType,
    Var,
    atan2,
    binary_min,
    build_module,
    div_floor,
    draw_circle,
    getloc_tl,
    isqrt,
    lengthdir,
    once,
    pow as eud_pow,
    rand_range,
    randomize,
    set_tech_researched,
    set_upgrade_level,
    srand,
    tech_researched,
    upgrade_level,
    when,
)


def prog() -> None:
    vx = Var(40)
    vy = Var(30)
    base = Var(3)
    neg_seven = Var(0xFFFFFFFF - 6)  # two's-complement -7

    q = div_floor(neg_seven, 3)
    root = isqrt(vx.mul(vx).add(vy.mul(vy)), 16)
    p = eud_pow(base, 3)
    angle = atan2(vy, vx)
    dx, dy = lengthdir(100, angle)
    found = binary_min(0, 15, lambda x: x.ge(10))

    srand(42)
    randomize()
    roll = rand_range(1, 6)

    loc = Location(0)
    loc.set_rect(64, 64, 192, 192)
    lx, ly = getloc_tl(0)

    # Emit 4 Marines on a build-time circle using Location 1 once.
    once(lambda: draw_circle(1, UnitType.TerranMarine, 0, 256, 256, radius=64, dots=4))

    out = Var(0)
    when([found.ge(10)], lambda: out.set(q.add(root).add(p).add(dx).add(dy).add(roll).add(lx).add(ly)))

    # Tech / upgrade state (0 = Stim Packs, 0 = Terran Infantry Armor/Weapons slot).
    once(lambda: (set_upgrade_level(0, 0, 2), set_tech_researched(0, 0, True)))
    up_lvl = upgrade_level(0, 0)
    when([tech_researched(0, 0), up_lvl.ge(2)], lambda: out.iadd(1))


module = build_module(prog)
