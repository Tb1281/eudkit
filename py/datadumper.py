"""The data dumper: a core helper port of eudplib's ``_add_datadumper`` /
``_f_datadumper`` (``eudlib/utilf/datadumper.py``) and euddraft's
``plugins/dataDumper.py``.

The helper injects a raw byte payload (an external file such as a
``stat_txt.tbl`` or a ``RequireData`` block) into the compiled module and makes
the game consume it. The payload lives in a module data blob (issue #99's
``Db`` / ``LirItem::Blob``); the helper only decides how the target memory
learns about it:

* **default** (no flag): ``SetMemory(out_offset, SetTo, db)`` — every target
  dword receives the blob's payload address.
* **``copy``**: the target dword holds the *destination's byte address* (the
  game's own pointer, e.g. the ``stat_txt.tbl`` pointer). The helper reads that
  dword at run time and converts it to an EPD (eudplib's ``f_epdread_epd``),
  then copies the payload there with ``memcpyEPD`` (eudplib's
  ``f_repmovsd_epd``), ``(len + 3) // 4`` dwords. This is the mode eudplib's
  own test uses: a ``RequireData`` file is copied to the address stored at
  ``0x58D740``.
* **``unpatchable``**: eudplib patches the target through
  ``f_dwpatch_epd``, which also records the previous value on a patch stack so
  ``f_unpatchall`` can restore it. eudkit has neither build-time memory
  patching nor a patch stack — every write it emits is already a run-time
  action — so the mode emits the same ``SetMemory`` write as the default mode.
  The two flags stay mutually exclusive, as in the source
  (``assert "copy" not in flags``).

The source splits registration (``_add_datadumper``) from emission
(``_f_datadumper``, run from ``onPluginStart``) because euddraft plugins
collect settings first. eudkit has no plugin system, so one
:func:`data_dumper` call does both, and the emission is wrapped in ``once``:
like the source's ``onPluginStart`` it runs on the first trigger loop and then
self-disables.

Deviations from the source
--------------------------
* **One blob per call.** The source's default and ``unpatchable`` branches
  call ``Db(input_data)`` inside the ``out_offsets`` loop, allocating one copy
  of the payload per target. The port allocates one blob and points every
  target at it: the bytes are identical and read-only, so the only difference
  is payload size (``len`` bytes instead of ``len * len(out_offsets)``).
* **The payload is padded to a whole dword.** ``copy`` moves
  ``(len + 3) // 4`` dwords, so the last dword's tail bytes are part of the
  write. eudplib's unpadded ``Db`` reads whatever payload item follows; the
  port pads the blob with zeros so the tail is deterministic. (A payload that
  is already dword-aligned is unchanged.)
* **An empty payload or an empty offset list is a no-op.** The source skips an
  empty offset list (``continue``) and would point at a zero-byte blob for an
  empty payload; the port returns ``None`` for the former and rejects the
  latter with ``EUDSL-RANGE``.

Cost
----
The default and ``unpatchable`` modes are one ``SetMemory`` act per target and
one ``once`` gate. ``copy`` costs one *EPD read* per target (eudplib's
``f_epdread_epd``: one accumulator reset, one bit test per bit from 2 up, and
the ``EPD_BASE`` subtraction) on top of the ``memcpyEPD`` loop (a check-first
``while`` with one copy trigger per dword). The payload itself is the dominant
part of the map-size cost.

The helper must run inside :func:`build_module` (creating the blob is the
scope gate, exactly as for ``Db``).
"""

from __future__ import annotations

from typing import Any, Iterable

from .blockio import memcpyEPD
from .hir import Db, SetMemory, SetTo
from .temporal import once
from .view import (
    EPD,
    EudslError,
    _describe_value as _describe,
    _require_plain_int,
    read_epd_pointer,
)

#: Copy the payload to the byte address stored in each ``out_offset`` (read at
#: run time; eudplib's ``f_epdread_epd`` converts it to an EPD).
FLAG_COPY = "copy"
#: Write the payload address with the source's patch routine. eudkit has no
#: patch stack, so this is the same write as the default mode.
FLAG_UNPATCHABLE = "unpatchable"

_FLAGS = frozenset({FLAG_COPY, FLAG_UNPATCHABLE})


def _require_payload(input_data: Any) -> bytes:
    if isinstance(input_data, (bytearray, memoryview)):
        input_data = bytes(input_data)
    if not isinstance(input_data, bytes):
        raise EudslError(
            "EUDSL-TYPE",
            f"dataDumper(): input_data must be bytes, got {_describe(input_data)}",
        )
    if not input_data:
        raise EudslError(
            "EUDSL-RANGE", "dataDumper(): input_data must be a non-empty byte string"
        )
    return input_data


def _require_out_offset(value: Any) -> int:
    offset = _require_plain_int(value, "dataDumper(): out_offset")
    if offset <= 0 or offset > 0xFFFFFFFC or offset % 4 != 0:
        raise EudslError(
            "EUDSL-BOUNDS",
            "dataDumper(): out_offset must be a positive 4-byte-aligned "
            f"address, got {offset}",
        )
    return offset


def _require_out_offsets(value: Any) -> list[int]:
    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, int):
        raise EudslError(
            "EUDSL-TYPE",
            f"dataDumper(): out_offsets must be a sequence of addresses, got {_describe(value)}",
        )
    try:
        offsets = list(value)
    except TypeError:
        raise EudslError(
            "EUDSL-TYPE",
            f"dataDumper(): out_offsets must be a sequence of addresses, got {_describe(value)}",
        ) from None
    return [_require_out_offset(offset) for offset in offsets]


def _require_flags(value: Any) -> frozenset[str]:
    if isinstance(value, str) or isinstance(value, (bytes, bytearray)):
        raise EudslError(
            "EUDSL-TYPE",
            "dataDumper(): flags must be a collection of flag names, got "
            f"{_describe(value)}",
        )
    try:
        flags = frozenset(value)
    except TypeError:
        raise EudslError(
            "EUDSL-TYPE",
            "dataDumper(): flags must be a collection of flag names, got "
            f"{_describe(value)}",
        ) from None
    for flag in flags:
        if not isinstance(flag, str) or flag not in _FLAGS:
            raise EudslError(
                "EUDSL-RANGE",
                "dataDumper(): unknown flag "
                f"{_describe(flag)}; expected 'copy' and/or 'unpatchable'",
            )
    if FLAG_COPY in flags and FLAG_UNPATCHABLE in flags:
        raise EudslError(
            "EUDSL-BOUNDS",
            "dataDumper(): cannot apply both 'copy' and 'unpatchable'",
        )
    return flags


def data_dumper(
    input_data: bytes,
    out_offsets: Iterable[int],
    flags: Iterable[str] = (),
) -> Db | None:
    """Inject ``input_data`` and point every ``out_offset`` at it.

    ``out_offsets`` are 4-byte-aligned memory addresses (byte addresses, not
    EPDs). With the ``copy`` flag each target dword holds the destination's
    byte address and is read at run time; without it the payload address is
    written into each target. ``unpatchable`` is accepted for source parity and
    writes the same address as the default mode (eudkit has no patch stack; see
    the module docstring). The two flags are mutually exclusive.

    The body runs on the first trigger loop (eudplib's ``onPluginStart``).
    Returns the payload blob, or ``None`` when ``out_offsets`` is empty.
    """
    payload = _require_payload(input_data)
    offsets = _require_out_offsets(out_offsets)
    flag_set = _require_flags(flags)
    if not offsets:
        return None

    dwords = (len(payload) + 3) // 4
    # Pad the tail dword so the `copy` mode's last rep-movsd dword reads
    # in-blob zeros instead of the next payload item (see the docstring).
    blob = Db(payload + b"\x00" * (dwords * 4 - len(payload)))

    if FLAG_COPY in flag_set:

        def copy_all() -> None:
            for offset in offsets:
                destination = read_epd_pointer(EPD(offset))
                memcpyEPD(destination, blob, dwords)

        once(copy_all)
        return blob

    # Default and `unpatchable`: the source writes the payload address into
    # each target and keeps the location for `f_unpatchall`. eudkit never
    # patches memory at build time, so the plain run-time write is the whole
    # observable behaviour.
    def patch_all() -> None:
        for offset in offsets:
            SetMemory(offset, SetTo, blob)

    once(patch_all)
    return blob


#: The canonical camelCase name (ADR 0002 §3); `data_dumper` is the Python
#: snake_case alias, mirroring `ts/datadumper.ts`.
dataDumper = data_dumper

__all__ = [
    "FLAG_COPY",
    "FLAG_UNPATCHABLE",
    "dataDumper",
    "data_dumper",
]
