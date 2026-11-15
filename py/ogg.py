"""Build-time Ogg length reader (port of tinytag's `_TinyTag` / `_Ogg`).

euddraft's ``lib/soundlooper.py`` carries a vendored copy of tinytag (MIT,
Tom Wallroth, 2014-2018) so it can measure each ``.ogg`` segment before
embedding it. eudkit has no plugin system, so that reader becomes a regular
build-time utility shared by the sound looper port and anyone else who needs
the duration of a Vorbis/Opus file.

The port keeps tinytag's page walk verbatim: the duration is the largest page
granule position divided by the stream's sample rate. Only the container
parsing (page headers, packet lacing, the identification and comment packets)
is needed, so the reader never decodes audio and never verifies CRCs.

Public surface
--------------
``read_ogg(path)`` reads a file from disk; ``parse_ogg(data)`` parses bytes that
are already in memory. Both return an :class:`OggInfo` with tinytag's metadata
fields plus ``duration`` (seconds, ``None`` when the file carries no audio
page). ``ogg_duration(path)`` is the one-call convenience form.

Deviations from tinytag
-----------------------
* A file object is replaced by an in-memory cursor over the bytes, so the
  reader has no dependency on ``BufferedReader.peek``/``seek`` quirks. The
  byte-by-byte tail scan, the packet lacing rules, and the metadata mapping
  are unchanged.
* Truncated or malformed structures raise :class:`OggError`
  (``EUDSL-FORMAT``) instead of ``struct.error``/``_TinyTagException``.
* ``round_duration`` rounds half away from zero to 3 decimals, shared verbatim
  with ``ts/ogg.ts`` so the Python and TypeScript frontends produce the same
  segment length. Python's built-in ``round`` is round-half-even and would
  disagree on an exact tie.
* The comment keys are decoded exactly as tinytag decodes them; a key/value
  pair that is not valid UTF-8 is skipped rather than failing the read.
"""

from __future__ import annotations

import math
import os
import struct
from typing import Optional

from .view import EudslError

#: The largest Ogg page (https://xiph.org/ogg/doc/libogg/ogg_page.html). The
#: duration scan starts this far before EOF, then walks forward to a page
#: boundary.
MAX_PAGE_SIZE = 65536

_PAGE_HEADER = struct.Struct("<4sBBqIIiB")

#: Vorbis comment key -> :class:`OggInfo` attribute (tinytag's mapping).
_COMMENT_FIELDS = {
    "album": "album",
    "albumartist": "albumartist",
    "title": "title",
    "artist": "artist",
    "date": "year",
    "tracknumber": "track",
    "discnumber": "disc",
    "genre": "genre",
    "description": "comment",
}


class OggError(EudslError):
    """Malformed or unreadable Ogg container (``EUDSL-FORMAT``)."""

    def __init__(self, message: str) -> None:
        super().__init__("EUDSL-FORMAT", f"ogg: {message}")


def round_duration(seconds: float) -> float:
    """Round ``seconds`` to 3 decimals, half away from zero.

    Shared verbatim with ``ts/ogg.ts``; see the module docstring.
    """
    return math.floor(seconds * 1000.0 + 0.5) / 1000.0


class _Cursor:
    """Minimal ``read``/``peek``/``tell``/``seek`` cursor over ``bytes``."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self, count: int) -> bytes:
        start = self.pos
        end = min(len(self.data), start + max(0, count))
        self.pos = end
        return self.data[start:end]

    def peek(self, count: int) -> bytes:
        return self.data[self.pos : self.pos + max(0, count)]

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> None:
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = self.pos + offset
        elif whence == os.SEEK_END:
            pos = len(self.data) + offset
        else:
            raise OggError(f"unknown seek mode {whence}")
        self.pos = min(max(pos, 0), len(self.data))


class OggInfo:
    """Metadata read from an Ogg container (tinytag's field names)."""

    def __init__(self) -> None:
        self.album: Optional[str] = None
        self.albumartist: Optional[str] = None
        self.artist: Optional[str] = None
        self.audio_offset: Optional[int] = None
        self.bitrate: Optional[float] = None
        self.channels: Optional[int] = None
        self.comment: Optional[str] = None
        self.disc: Optional[str] = None
        self.disc_total: Optional[str] = None
        self.duration: Optional[float] = None
        self.genre: Optional[str] = None
        self.samplerate: Optional[int] = None
        self.title: Optional[str] = None
        self.track: Optional[str] = None
        self.track_total: Optional[str] = None
        self.year: Optional[str] = None

    def _set_field(
        self,
        fieldname: str,
        value: str,
    ) -> None:
        """Store a comment value, splitting ``track``/``disc`` totals."""
        if getattr(self, fieldname):  # do not overwrite existing data
            return
        if fieldname in ("track", "disc"):
            if "/" in value:
                current, total = value.split("/")[:2]
                setattr(self, f"{fieldname}_total", total)
            else:
                current = value
            setattr(self, fieldname, current)
        else:
            setattr(self, fieldname, value)


class _OggReader(OggInfo):
    def __init__(self, cursor: _Cursor, filesize: int) -> None:
        super().__init__()
        self._cursor = cursor
        self.filesize = filesize
        self._tags_parsed = False
        self._max_samplenum = 0  # maximum sample position ever read

    def load(self) -> None:
        self._determine_duration(self._cursor)

    def _determine_duration(self, fh: _Cursor) -> None:
        if not self._tags_parsed:
            self._parse_tag(fh)  # determine sample rate
            self._tags_parsed = True
            fh.seek(0)  # and rewind to start
        if self.filesize > MAX_PAGE_SIZE:
            fh.seek(-MAX_PAGE_SIZE, os.SEEK_END)  # last possible page position
        while True:
            b = fh.peek(4)
            if len(b) == 0:
                return  # EOF
            if b[:4] == b"OggS":  # look for an ogg header
                for _packet in self._parse_pages(fh):
                    pass  # parse all remaining pages
                if self.samplerate is None:
                    raise OggError("audio stream has no sample rate")
                self.duration = self._max_samplenum / float(self.samplerate)
            else:
                idx = b.find(b"OggS")  # try to find header in peeked data
                seekpos = idx if idx != -1 else len(b) - 3
                fh.seek(max(seekpos, 1), os.SEEK_CUR)

    def _parse_tag(self, fh: _Cursor) -> None:
        page_start_pos = fh.tell()  # set audio_offset later if it is audio
        for packet in self._parse_pages(fh):
            walker = _PacketCursor(packet)
            if packet[0:7] == b"\x01vorbis":
                if len(packet) < 28:
                    raise OggError("truncated vorbis identification packet")
                (
                    channels,
                    self.samplerate,
                    _max_bitrate,
                    bitrate,
                    _min_bitrate,
                ) = struct.unpack("<B4i", packet[11:28])
                if not self.audio_offset:
                    self.bitrate = bitrate / 1024.0
                    self.audio_offset = page_start_pos
            elif packet[0:7] == b"\x03vorbis":
                walker.seek(7)  # jump over header name
                self._parse_vorbis_comment(walker)
            elif packet[0:8] == b"OpusHead":  # parse opus header
                walker.seek(8)  # jump over header name
                head = walker.read(11)
                if len(head) < 11:
                    raise OggError("truncated opus header")
                version, ch, _pre_skip, sr, _gain, _family = struct.unpack(
                    "<BBHIHB", head
                )
                if (version & 0xF0) == 0:  # only major version 0 supported
                    self.channels = ch
                    self.samplerate = sr
            elif packet[0:8] == b"OpusTags":  # parse opus metadata
                walker.seek(8)
                self._parse_vorbis_comment(walker)
            else:
                break
            page_start_pos = fh.tell()

    def _parse_vorbis_comment(self, fh: "_PacketCursor") -> None:
        # For the spec, see: http://xiph.org/vorbis/doc/v-comment.html
        vendor_length = _unpack_u32(fh, "vorbis vendor length")
        fh.seek(vendor_length, os.SEEK_CUR)  # jump over vendor
        elements = _unpack_u32(fh, "vorbis comment count")
        for _ in range(elements):
            length = _unpack_u32(fh, "vorbis comment length")
            raw = fh.read(length)
            if len(raw) != length:
                raise OggError("truncated vorbis comment")
            try:
                keyvalpair = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if "=" in keyvalpair:
                key, value = keyvalpair.split("=", 1)
                fieldname = _COMMENT_FIELDS.get(key.lower())
                if fieldname:
                    self._set_field(fieldname, value)

    def _parse_pages(self, fh: _Cursor):
        # For the spec, see: https://wiki.xiph.org/Ogg
        previous_page = b""  # data carried over from continuing pages
        header_data = fh.read(_PAGE_HEADER.size)  # read an ogg page header
        while len(header_data) != 0:
            if len(header_data) != _PAGE_HEADER.size:
                raise OggError("truncated ogg page header")
            oggs, version, _flags, pos, _serial, _pageseq, _crc, segments = (
                _PAGE_HEADER.unpack(header_data)
            )
            self._max_samplenum = max(self._max_samplenum, pos)
            if oggs != b"OggS" or version != 0:
                raise OggError("not a valid ogg file")
            raw_segs = fh.read(segments)
            if len(raw_segs) != segments:
                raise OggError("truncated ogg segment table")
            segsizes = struct.unpack(f"{segments}B", raw_segs) if segments else ()
            total = 0
            for segsize in segsizes:  # read all segments
                total += segsize
                if total < 255:  # less than 255 bytes means end of packet
                    payload = fh.read(total)
                    if len(payload) != total:
                        raise OggError("truncated ogg packet")
                    yield previous_page + payload
                    previous_page = b""
                    total = 0
            if total != 0:
                if total % 255 == 0:
                    payload = fh.read(total)
                    if len(payload) != total:
                        raise OggError("truncated ogg packet")
                    previous_page += payload
                else:
                    payload = fh.read(total)
                    if len(payload) != total:
                        raise OggError("truncated ogg packet")
                    yield previous_page + payload
                    previous_page = b""
            header_data = fh.read(_PAGE_HEADER.size)


class _PacketCursor:
    """Cursor over one packet payload (tinytag's per-packet ``BytesIO``)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self, count: int) -> bytes:
        start = self.pos
        end = min(len(self.data), start + max(0, count))
        self.pos = end
        return self.data[start:end]

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> None:
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = self.pos + offset
        elif whence == os.SEEK_END:
            pos = len(self.data) + offset
        else:
            raise OggError(f"unknown seek mode {whence}")
        self.pos = min(max(pos, 0), len(self.data))


def _unpack_u32(fh: _PacketCursor, what: str) -> int:
    raw = fh.read(4)
    if len(raw) != 4:
        raise OggError(f"truncated {what}")
    return struct.unpack("<I", raw)[0]


def parse_ogg(data: bytes | bytearray | memoryview) -> OggInfo:
    """Read metadata (including ``duration``) from in-memory Ogg bytes."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise OggError(f"expected bytes, got {type(data).__name__}")
    payload = bytes(data)
    if not payload:
        return OggInfo()
    reader = _OggReader(_Cursor(payload), len(payload))
    reader.load()
    return reader


def read_ogg(path: str) -> OggInfo:
    """Read metadata (including ``duration``) from ``path``."""
    if not isinstance(path, str) or not path:
        raise OggError("a non-empty path is required")
    try:
        size = os.path.getsize(path)
    except OSError as error:
        raise OggError(f"cannot read {path!r}: {error}") from None
    if size == 0:
        return OggInfo()
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise OggError(f"cannot read {path!r}: {error}") from None
    return parse_ogg(data)


def ogg_duration(path: str) -> Optional[float]:
    """The stream duration in seconds, or ``None`` for an empty file."""
    return read_ogg(path).duration


__all__ = [
    "MAX_PAGE_SIZE",
    "OggError",
    "OggInfo",
    "ogg_duration",
    "parse_ogg",
    "read_ogg",
    "round_duration",
]
