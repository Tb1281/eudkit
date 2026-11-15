from py import (
    ActView,
    FixedArray,
    CSprite,
    CUnit,
    CondView,
    Deaths,
    Deque,
    Exactly,
    Func,
    PageSlotEPD,
    Queue,
    SetDeaths,
    SetTo,
    Stack,
    FixedString,
    MemCond,
    Trigger,
    TriggerStruct,
    UnitGroup,
    Var,
    after,
    arrayDword,
    bit,
    build_module,
    byte,
    dword,
    every,
    memcpyEPD,
    memsetEPD,
    onFalling,
    onRising,
    once,
    until,
    when,
    whileLoop,
    withCP,
    word,
)
from py.view import make_memview

Point = TriggerStruct("Point", {"x": dword(), "y": word()})
Hero = TriggerStruct(
    "Hero",
    {"hp": dword(), "mp": word(), "armor": byte(), "flags": bit(4)},
)


def cp_body(cp):
    cp.at(2).set(100)
    viewed = cp.view(CUnit)
    viewed.hp.iadd(5)
    viewed.shield.set(800)


def prog():
    # TriggerStruct: whole-struct copy plus direct field access/mutation.
    hero_src = Hero(hp=1000, mp=0x1234, armor=0xAB, flags=0x0D)
    hero_dst = Hero()
    hero_dst.copyFrom(hero_src)
    hero_dst.hp.iadd(5)
    hero_dst.armor.set(0x42)
    hp_out = Var(0)
    hp_out.set(hero_dst.hp)

    point_src = Point(x=7, y=9)
    point_dst = Point()
    point_dst.set(point_src)
    point_dst.x.set(8)

    # MemView layouts: CUnit and CSprite fields resolve to raw EPDs.
    unit = CUnit(1000)
    unit.hp.set(777)
    unit.posY.set(300)
    sprite = CSprite(2000)
    sprite.index.set(11)

    # Memory primitives: eq / ne / read / MemCond.
    when(CUnit(1000).hp.eq(500), lambda: SetDeaths(9000, SetTo, 1, 0))
    when(CUnit(1000).hp.ne(500), lambda: SetDeaths(9000, SetTo, 1, 0))
    read_out = Var(0)
    read_out.set(CUnit(1000).hp.read())
    when(MemCond(3000, "atleast", 5), lambda: SetDeaths(9000, SetTo, 1, 0))

    # Standalone CondView / ActView slots through the generic view constructor.
    cond_slot = make_memview(CondView, 500)
    cond_slot.amount.set(17)
    cond_slot.comparison.set(Exactly)
    act_slot = make_memview(ActView, 600)
    act_slot.value.set(42)
    act_slot.acttype.set(45)

    # withCP: CurrentPlayer binding, relative hops, and cursor restore.
    withCP(7000, cp_body)

    # FixedArray: writable storage, const table, and dynamic PageSlotEPD index.
    arr = FixedArray(arrayDword, 4)
    arr.set(0, 11)
    arr.set(3, 44)
    index = Var(3)
    dyn_out = Var(0)
    dyn_out.set(arr.get(index))
    table = FixedArray.const([10, 20, 30, 40])
    const_out = Var(0)
    when(table.get(2).eq(30), lambda: const_out.set(1))
    epd_base = Var(100)
    computed = PageSlotEPD(epd_base, index, 3)
    epd_out = Var(0)
    epd_out.set(computed)

    # Fixed-out-box containers.
    queue = Queue("dword", 2)
    queue.append(11)
    queue.append(22)
    queue_out = Var(0)
    queue_out.set(queue.popleft())

    deque = Deque("word", 2)
    deque.append(1)
    deque.appendleft(2)
    deque_out = Var(0)
    deque_out.set(deque.pop())

    stack = Stack(2)
    stack.push(11)
    stack.push(22)
    stack_out = Var(0)
    stack_out.set(stack.pop())

    group = UnitGroup(2)
    group.add(1000)
    group.remove(1000)

    # STR-backed FixedString buffer with static writes and a display action.
    text = FixedString(16)
    text.write("cost-gate")
    text.append("!")
    text.display()

    # ADR 0011 dynamic fixed-width hex formatting: a two-variable HUD counter.
    hud = FixedString(32)
    wave = Var(1)
    hp = Var(100)
    hud.write("wave={:x} hp={:x}", wave, hp)

    # Block memory primitives.
    memsetEPD(3000, 0xAABBCCDD, 2)
    memcpyEPD(4000, 3000, 2)

    # Variable-count forms keep the runtime loops.
    n = Var(2)
    memsetEPD(3000, 0x11223344, n)
    memcpyEPD(6000, 3000, n)

    # Every temporal form.
    tick = Var(0)
    flag = Var(0)
    when(tick.eq(0), lambda: flag.set(1))
    once(flag.eq(1), lambda: tick.iadd(1))
    until(flag.eq(1), lambda: tick.iadd(1))
    onRising(flag.eq(1), lambda: tick.iadd(1))
    onFalling(flag.eq(0), lambda: tick.iadd(1))
    every(2, lambda: tick.iadd(1))
    after(2, lambda: tick.iadd(1))
    whileLoop(tick.lt(2), lambda: tick.iadd(1))
    bump = Func(lambda: tick.iadd(1))
    bump()

    # One explicitly packed 16-condition / 64-action trigger.
    packed_conds = []
    packed_acts = []
    for i in range(16):
        packed_conds.append(Deaths(2000 + i, Exactly, i, 0))
    for i in range(64):
        packed_acts.append(SetDeaths(5000 + i, SetTo, i + 1, 0))
    packed = Trigger(conds=packed_conds, acts=packed_acts)
    packed.cond(0).amount.set(500)
    packed.act(0).value.set(999)


module = build_module(prog)
