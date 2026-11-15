"""First-class `Trigger (16C, 64A)` and `TriggerPage` primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .view import EudslError, TriggerFieldRef
from .conditions import (
    PlanContext,
    build_plan,
    emit_plan,
    plan_cond_count,
)

COND_COUNT = 16
COND_BASE_BYTES = 8
COND_STRIDE_BYTES = 20
COND_STRIDE_EPDS = 5

ACT_COUNT = 64
ACT_BASE_BYTES = 328
ACT_STRIDE_BYTES = 32
ACT_STRIDE_EPDS = 8

TRIGGER_FLAGS_OFFSET_BYTES = 2376
TRIGGER_SIZE_BYTES = 2408
TRIGGER_STRIDE_EPDS = 602


def pageSlotEpdWeight(bit: int) -> int:
    """Carry-free EPD weight of bit `b` for a `64A` `TriggerPage` index."""
    if not isinstance(bit, int) or isinstance(bit, bool) or not (0 <= bit < 32):
        raise EudslError("EUDSL-BOUNDS", f"pageSlotEpdWeight: bit must be in 0..31, got {bit!r}")
    return (ACT_STRIDE_EPDS << bit) if bit < 6 else (TRIGGER_STRIDE_EPDS << (bit - 6))


@dataclass(frozen=True)
class ActionSlotInit:
    slot: int
    mask: int
    value: int
    selfTargeted: bool = True


class TriggerPage:
    """A 64-action `Trigger (16C, 64A)` storage page enforcing the self-targeted action invariant."""

    def __init__(
        self,
        initial_values: Sequence[int] = (),
        masks: Sequence[int] | None = None,
    ) -> None:
        if len(initial_values) > ACT_COUNT:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"TriggerPage holds at most {ACT_COUNT} action slots, got {len(initial_values)}",
            )
        self.slots: list[ActionSlotInit] = [
            ActionSlotInit(
                slot=k,
                mask=((masks[k] if masks and k < len(masks) else 0xFFFFFFFF) & 0xFFFFFFFF),
                value=(initial_values[k] if k < len(initial_values) else 0) & 0xFFFFFFFF,
                selfTargeted=True,
            )
            for k in range(ACT_COUNT)
        ]


class CondSlotView:
    """Bound 20-byte (`5`-EPD) `CondView` slot returned by `trig.cond(i)`."""

    BYTE_SIZE = COND_STRIDE_BYTES
    EPD_SIZE = COND_STRIDE_EPDS

    def __init__(
        self,
        trigger_id: int,
        index: int,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.triggerId = trigger_id
        self.index = index
        self.byteOffset = COND_BASE_BYTES + index * COND_STRIDE_BYTES
        self.epdOffset = self.byteOffset // 4
        self.data = data

        base = self.byteOffset
        self.mask = TriggerFieldRef(trigger_id, base + 0, 4, int(data["locid"]) if data is not None else 0)
        self.locid = self.mask
        self.player = TriggerFieldRef(trigger_id, base + 4, 4, int(data["player"]) if data is not None else 0)
        self.amount = TriggerFieldRef(trigger_id, base + 8, 4, int(data["amount"]) if data is not None else 0)
        self.unit = TriggerFieldRef(trigger_id, base + 12, 2, int(data["unit"]) if data is not None else 0)
        self.comparison = TriggerFieldRef(trigger_id, base + 14, 1, int(data["comparison"]) if data is not None else 0)
        self.condtype = TriggerFieldRef(trigger_id, base + 15, 1, int(data["condtype"]) if data is not None else 0)
        self.restype = TriggerFieldRef(trigger_id, base + 16, 1, int(data["restype"]) if data is not None else 0)
        self.flags = TriggerFieldRef(trigger_id, base + 17, 1, int(data["flags"]) if data is not None else 0)
        self.eudx = TriggerFieldRef(trigger_id, base + 18, 2, int(data["eudx"]) if data is not None else 0)


class ActSlotView:
    """Bound 32-byte (`8`-EPD) `ActView` slot returned by `trig.act(k)`."""

    BYTE_SIZE = ACT_STRIDE_BYTES
    EPD_SIZE = ACT_STRIDE_EPDS

    def __init__(
        self,
        trigger_id: int,
        index: int,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.triggerId = trigger_id
        self.index = index
        self.byteOffset = ACT_BASE_BYTES + index * ACT_STRIDE_BYTES
        self.epdOffset = self.byteOffset // 4
        self.data = data

        base = self.byteOffset
        self.mask = TriggerFieldRef(trigger_id, base + 0, 4, int(data["mask"]) if data is not None else 0)
        self.locid1 = self.mask
        self.strid = TriggerFieldRef(trigger_id, base + 4, 4, int(data["strid"]) if data is not None else 0)
        self.wavid = TriggerFieldRef(trigger_id, base + 8, 4, int(data["wavid"]) if data is not None else 0)
        self.time = TriggerFieldRef(trigger_id, base + 12, 4, int(data["time"]) if data is not None else 0)
        self.dest = TriggerFieldRef(trigger_id, base + 16, 4, int(data["dest"]) if data is not None else 0)
        self.player1 = self.dest
        self.value = TriggerFieldRef(trigger_id, base + 20, 4, int(data["value"]) if data is not None else 0)
        self.player2 = self.value
        self.unit = TriggerFieldRef(trigger_id, base + 24, 2, int(data["unit"]) if data is not None else 0)
        self.acttype = TriggerFieldRef(trigger_id, base + 26, 1, int(data["acttype"]) if data is not None else 0)
        self.modifier = TriggerFieldRef(trigger_id, base + 27, 1, int(data["modifier"]) if data is not None else 0)
        self.amount = self.modifier
        self.flags = TriggerFieldRef(trigger_id, base + 28, 1, int(data["flags"]) if data is not None else 0)
        self.pad = TriggerFieldRef(trigger_id, base + 29, 1, int(data["padding"]) if data is not None else 0)
        self.padding = self.pad
        self.eudx = TriggerFieldRef(trigger_id, base + 30, 2, int(data["eudx"]) if data is not None else 0)


# Builder seam slot (candidate 8, shape C): this module owns ONLY the
# module-global active-builder slot and its getter/setter. The hook
# implementation and all mutable builder state live with ``buildModule``
# in ``hir.py`` (``_ModuleBuilderState``); see its class docstring for
# the ordering guarantees (registration position, flush-before-scope-change,
# scope serial bumps, module-end flush). Do NOT move the slot into
# ``hir.py``: the ``Trigger`` constructor needs the getter, and a
# trigger -> hir runtime import would create a real top-level cycle
# (``hir`` imports ``trigger`` at top level).
_active_module_builder: Any = None
_fallback_trigger_counter: int = 0


def _set_active_module_builder(builder: Any) -> Any:
    global _active_module_builder
    prev = _active_module_builder
    _active_module_builder = builder
    return prev


def _get_active_module_builder() -> Any:
    return _active_module_builder


def _normalize_ptr(ptr: Any) -> dict[str, Any] | None:
    if ptr is None:
        return None
    if isinstance(ptr, int) and not isinstance(ptr, bool):
        return {"kind": "const", "value": ptr}
    if isinstance(ptr, Trigger):
        return {"kind": "trigger", "id": ptr.id, "offset": 0}
    raise TypeError(f"Invalid trigger pointer target: {ptr!r}")


class Trigger:
    """First-class `Trigger (16C, 64A)` domain primitive."""

    COND_COUNT = COND_COUNT
    ACT_COUNT = ACT_COUNT
    BYTE_SIZE = TRIGGER_SIZE_BYTES
    EPD_SIZE = TRIGGER_STRIDE_EPDS

    def __init__(
        self,
        opts: Mapping[str, Any] | None = None,
        *,
        conds: Sequence[Any] | None = None,
        acts: Sequence[Mapping[str, Any]] | None = None,
        preserved: bool | None = None,
        flags: int | None = None,
        prev: Trigger | int | None = None,
        next: Trigger | int | None = None,
        id: int | None = None,
        register: bool = True,
    ) -> None:
        global _fallback_trigger_counter

        if opts is not None:
            if not isinstance(opts, Mapping):
                raise TypeError("Trigger first positional argument must be an options mapping")
            if conds is None and "conds" in opts:
                conds = opts["conds"]
            if acts is None and "acts" in opts:
                acts = opts["acts"]
            if preserved is None and "preserved" in opts:
                preserved = opts["preserved"]
            if flags is None and "flags" in opts:
                flags = opts["flags"]
            if prev is None and "prev" in opts:
                prev = opts["prev"]
            if next is None and "next" in opts:
                next = opts["next"]
            if id is None and "id" in opts:
                id = opts["id"]

        # A raw Trigger is itself an evaluation site.  Plan (validate) the
        # whole condition list and count expanded slots BEFORE emitting any
        # prelude, so an over-cap trigger cannot leak flag Vars or reset/
        # set-when triggers into the enclosing build_module.
        raw_conds = list(conds) if conds is not None else []
        act_list = list(acts) if acts is not None else []
        ctx = PlanContext()
        plan = build_plan(raw_conds, ctx)
        planned = plan_cond_count(plan)

        if planned > COND_COUNT:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Trigger condition count {planned} exceeds maximum {COND_COUNT}",
            )
        if len(act_list) > ACT_COUNT:
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Trigger action count {len(act_list)} exceeds maximum {ACT_COUNT}",
            )
        cond_list = emit_plan(plan, ctx)

        builder = _active_module_builder
        if id is not None:
            self.id = id
            if builder is not None:
                builder.consume_queued_acts(act_list)
        elif builder is not None:
            builder.consume_queued_acts(act_list)
            self.id = builder.alloc_trigger_id()
        else:
            self.id = _fallback_trigger_counter
            _fallback_trigger_counter += 1

        self.conds: tuple[Mapping[str, Any], ...] = tuple(cond_list)
        self.acts: tuple[Mapping[str, Any], ...] = tuple(act_list)
        self.preserved: bool = True if preserved is None else bool(preserved)
        self.flags: int = (
            (flags if flags is not None else (4 if self.preserved else 0)) & 0xFFFFFFFF
        )
        self.prevPtr = _normalize_ptr(prev)
        self.nextPtr = _normalize_ptr(next)

        self.prev = TriggerFieldRef(self.id, 0, 4, 0)
        self.next = TriggerFieldRef(self.id, 4, 4, 0)
        self.flagsField = TriggerFieldRef(
            self.id,
            TRIGGER_FLAGS_OFFSET_BYTES,
            4,
            self.flags,
        )

        if builder is not None and register:
            builder.register_trigger(self)

    def condByteOffset(self, index: int) -> int:
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < COND_COUNT):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Trigger.cond index must be in 0..15, got {index!r}",
            )
        return COND_BASE_BYTES + index * COND_STRIDE_BYTES

    def actByteOffset(self, index: int) -> int:
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < ACT_COUNT):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Trigger.act index must be in 0..63, got {index!r}",
            )
        return ACT_BASE_BYTES + index * ACT_STRIDE_BYTES

    def cond(self, index: int) -> CondSlotView:
        """Direct condition slot inspection (`0 <= index < 16`)."""
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < COND_COUNT):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Trigger.cond index must be in 0..15, got {index!r}",
            )
        data = self.conds[index] if index < len(self.conds) else None
        return CondSlotView(self.id, index, data)

    def act(self, index: int) -> ActSlotView:
        """Direct action slot inspection (`0 <= index < 64`)."""
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < ACT_COUNT):
            raise EudslError(
                "EUDSL-BOUNDS",
                f"Trigger.act index must be in 0..63, got {index!r}",
            )
        data = self.acts[index] if index < len(self.acts) else None
        return ActSlotView(self.id, index, data)

    def fire(self) -> dict[str, Trigger]:
        """Splice through this trigger once and return to the continuation trigger in 1T executed."""
        global _fallback_trigger_counter
        builder = _active_module_builder
        cont_id = builder.alloc_trigger_id() if builder is not None else _fallback_trigger_counter
        if builder is None:
            _fallback_trigger_counter += 1
        dispatch = Trigger(
            acts=[
                {
                    "mask": 0,
                    "strid": 0,
                    "wavid": 0,
                    "time": 0,
                    "dest": 0,
                    "value": 0,
                    "unit": 0,
                    "acttype": 45,
                    "modifier": 7,
                    "flags": 0,
                    "padding": 0,
                    "eudx": 0,
                    "relocs": [
                        {
                            "field_offset": 16,
                            "target_trigger": self.id,
                            "target_offset": 4,
                            "mode": 1,
                        },
                        {
                            "field_offset": 20,
                            "target_trigger": cont_id,
                            "target_offset": 0,
                            "mode": 4,
                        },
                    ],
                }
            ],
            next=self,
        )
        cont = Trigger(id=cont_id, preserved=True)
        return {"dispatch": dispatch, "continuation": cont}


def _register_trigger(trig: Trigger) -> None:
    """Register a trigger previously created with ``register=False``.

    This is the seam's explicit-registration API, NOT a bypass around
    the builder: with an active builder it calls
    ``builder.register_trigger(trig)``, and with no active builder it is
    a no-op. Call sites (queue probe, array pointer, blockio pointers,
    string pointer) construct the trigger unregistered, patch its slots
    (e.g. ``emitVarStore`` into its fields), then register it explicitly
    so its HIR position follows its setup acts instead of preceding
    them. A trigger's HIR position is therefore its registration
    position; construction alone never places it.
    """
    builder = _active_module_builder
    if builder is not None:
        builder.register_trigger(trig)
