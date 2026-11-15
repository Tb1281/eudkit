"""Build-time Ogg length reader (`py/ogg.py`), the tinytag port used by the
sound looper. Mirror of ``ts/tests/test_ogg.ts``.

The fixtures under ``tests/fixtures/ogg`` are tiny hand-built Ogg streams: an
identification page, a comment page, and an audio page whose granule position
is the duration times the sample rate. They carry no decodable audio because
the reader never decodes audio.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from py.ogg import (
    OggError,
    OggInfo,
    ogg_duration,
    parse_ogg,
    read_ogg,
    round_duration,
)

FIXTURES = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"


def _page(
    serial: int,
    seq: int,
    granule: int,
    packet: bytes,
    flags: int,
) -> bytes:
    segs: list[int] = []
    remaining = len(packet)
    while remaining >= 255:
        segs.append(255)
        remaining -= 255
    segs.append(remaining)
    header = b"OggS" + bytes([0, flags])
    header += struct.pack("<qIIiB", granule, serial, seq, 0, len(segs))
    return header + bytes(segs) + packet


def _identification(samplerate: int, channels: int = 2) -> bytes:
    return (
        b"\x01vorbis"
        + struct.pack("<I", 0)
        + bytes([channels])
        + struct.pack("<i", samplerate)
        + struct.pack("<iii", 0, 128000, 0)
        + bytes([0xB8, 1])
    )


def _comment(entries: list[str], vendor: bytes = b"eudkit") -> bytes:
    body = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(entries))
    for entry in entries:
        encoded = entry.encode("utf-8")
        body += struct.pack("<I", len(encoded)) + encoded
    return b"\x03vorbis" + body + b"\x01"


def _stream(
    samplerate: int,
    granule: int,
    entries: list[str] | None = None,
    channels: int = 2,
) -> bytes:
    serial = 0x1234
    return (
        _page(serial, 0, 0, _identification(samplerate, channels), 0x02)
        + _page(serial, 1, 0, _comment(entries or []), 0x00)
        + _page(serial, 2, granule, b"\x00" * 8, 0x04)
    )


def _fixture(name: str) -> Path:
    return FIXTURES / "ogg" / name


class TestRoundDuration(unittest.TestCase):
    def test_rounds_to_three_decimals(self):
        self.assertEqual(round_duration(1.2317687074829933), 1.232)
        self.assertEqual(round_duration(1.0), 1.0)
        self.assertEqual(round_duration(1.5), 1.5)

    def test_ties_round_half_away_from_zero(self):
        # Shared rule with ts/ogg.ts; Python's round() would give 1.234 here.
        self.assertEqual(round_duration(1.2345), 1.235)
        self.assertEqual(round_duration(2.0005), 2.001)


class TestOggFixtures(unittest.TestCase):
    def test_known_fixture_durations(self):
        cases = {
            "one_second.ogg": 1.0,
            "one_and_half.ogg": 1.5,
            "two_seconds.ogg": 2.0,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                info = read_ogg(str(_fixture(name)))
                self.assertEqual(info.duration, expected)
                self.assertEqual(info.samplerate, 48000)
                self.assertEqual(round_duration(info.duration), expected)

    def test_odd_length_fixture_uses_the_granule_ratio(self):
        info = read_ogg(str(_fixture("odd_length.ogg")))
        self.assertEqual(info.samplerate, 44100)
        self.assertAlmostEqual(info.duration, 54321 / 44100, places=12)
        self.assertEqual(round_duration(info.duration), 1.232)

    def test_identification_header_fields(self):
        info = read_ogg(str(_fixture("one_second.ogg")))
        # tinytag maps the nominal bitrate to kibibit/s and records the page
        # start as the audio offset.
        self.assertEqual(info.bitrate, 125.0)
        self.assertEqual(info.audio_offset, 0)
        # tinytag never assigns the vorbis channel count (source parity).
        self.assertIsNone(info.channels)

    def test_parse_bytes_and_read_file_agree(self):
        data = _fixture("one_and_half.ogg").read_bytes()
        self.assertEqual(parse_ogg(data).duration, 1.5)
        self.assertEqual(parse_ogg(memoryview(data)).duration, 1.5)
        self.assertEqual(ogg_duration(str(_fixture("one_and_half.ogg"))), 1.5)


class TestOggTags(unittest.TestCase):
    def test_vorbis_comment_fields(self):
        data = _stream(
            48000,
            48000,
            [
                "TITLE=Loop Theme",
                "artist=Someone",
                "date=2026",
                "tracknumber=07/12",
                "discnumber=1/2",
                "genre=Game",
                "description=notes",
                "album=Album",
                "albumartist=Various",
            ],
        )
        info = parse_ogg(data)
        self.assertEqual(info.title, "Loop Theme")
        self.assertEqual(info.artist, "Someone")
        self.assertEqual(info.year, "2026")
        self.assertEqual(info.track, "07")
        self.assertEqual(info.track_total, "12")
        self.assertEqual(info.disc, "1")
        self.assertEqual(info.disc_total, "2")
        self.assertEqual(info.genre, "Game")
        self.assertEqual(info.comment, "notes")
        self.assertEqual(info.album, "Album")
        self.assertEqual(info.albumartist, "Various")

    def test_unknown_keys_and_first_value_wins(self):
        data = _stream(
            48000,
            48000,
            ["TITLE=first", "TITLE=second", "custom=ignored", "=empty"],
        )
        info = parse_ogg(data)
        self.assertEqual(info.title, "first")

    def test_non_utf8_comment_value_is_skipped(self):
        vendor = b"eudkit"
        bad = b"TITLE=\xff"
        body = (
            struct.pack("<I", len(vendor))
            + vendor
            + struct.pack("<I", 2)
            + struct.pack("<I", len(bad))
            + bad
            + struct.pack("<I", len(b"ARTIST=ok"))
            + b"ARTIST=ok"
        )
        packet = b"\x03vorbis" + body + b"\x01"
        data = (
            _page(1, 0, 0, _identification(48000), 0x02)
            + _page(1, 1, 0, packet, 0x00)
            + _page(1, 2, 48000, b"\x00" * 8, 0x04)
        )
        info = parse_ogg(data)
        self.assertIsNone(info.title)
        self.assertEqual(info.artist, "ok")


class TestOggEdgeCases(unittest.TestCase):
    def test_empty_file_has_no_duration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            empty = Path(tmpdir) / "empty.ogg"
            empty.write_bytes(b"")
            info = read_ogg(str(empty))
        self.assertIsInstance(info, OggInfo)
        self.assertIsNone(info.duration)

    def test_bad_version_is_a_format_error(self):
        data = bytearray(_stream(48000, 48000))
        data[4] = 1  # page version must be 0
        with self.assertRaises(OggError) as caught:
            parse_ogg(bytes(data))
        self.assertEqual(caught.exception.code, "EUDSL-FORMAT")
        self.assertIn("not a valid ogg file", str(caught.exception))

    def test_truncated_page_header_is_a_format_error(self):
        data = _stream(48000, 48000) + b"OggS\x00"
        with self.assertRaises(OggError) as caught:
            parse_ogg(data)
        self.assertIn("truncated ogg page header", str(caught.exception))

    def test_missing_file_is_a_format_error(self):
        with self.assertRaises(OggError) as caught:
            read_ogg(str(FIXTURES / "ogg" / "nope.ogg"))
        self.assertIn("cannot read", str(caught.exception))

    def test_non_ogg_data_is_a_format_error(self):
        # The reader starts at offset 0 (it never hunts for a later "OggS"),
        # so a page whose first bytes are not the capture pattern is rejected.
        with self.assertRaises(OggError) as caught:
            parse_ogg(b"not an ogg stream" + b"\x00" * 20)
        self.assertIn("not a valid ogg file", str(caught.exception))

    def test_short_non_ogg_data_is_a_truncated_header(self):
        with self.assertRaises(OggError) as caught:
            parse_ogg(b"not an ogg stream")
        self.assertIn("truncated ogg page header", str(caught.exception))

    def test_large_file_scan_finds_the_last_granule(self):
        # One page per 64 KiB chunk keeps the file above MAX_PAGE_SIZE so the
        # scan starts at the tail window and still reports the final granule.
        pages = [_page(7, 0, 0, _identification(48000), 0x02)]
        payload = b"\x00" * 300
        for seq in range(1, 400):
            pages.append(_page(7, seq, seq * 480, payload, 0x00))
        data = b"".join(pages)
        self.assertGreater(len(data), 65536)
        info = parse_ogg(data)
        self.assertEqual(info.duration, 399 * 480 / 48000)


if __name__ == "__main__":
    unittest.main()
