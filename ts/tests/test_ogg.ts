// Build-time Ogg length reader (`ts/ogg.ts`), the tinytag port used by the
// sound looper. Mirror of `py/tests/test_ogg.py`.
//
// The fixtures under `tests/fixtures/ogg` are tiny hand-built Ogg streams: an
// identification page, a comment page, and an audio page whose granule
// position is the duration times the sample rate. They carry no decodable
// audio because the reader never decodes audio.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  OggError,
  OggInfo,
  oggDuration,
  parseOgg,
  readOgg,
  roundDuration,
} from "../index.ts";

const FIXTURES = path.resolve(import.meta.dirname, "..", "..", "tests", "fixtures");

function u32(value: number): number[] {
  return [value & 0xff, (value >>> 8) & 0xff, (value >>> 16) & 0xff, (value >>> 24) & 0xff];
}

function i32(value: number): number[] {
  return u32(value >>> 0);
}

function page(serial: number, seq: number, granule: number, packet: Uint8Array, flags: number): Uint8Array {
  const segs: number[] = [];
  let remaining = packet.length;
  while (remaining >= 255) {
    segs.push(255);
    remaining -= 255;
  }
  segs.push(remaining);
  const parts: number[] = [0x4f, 0x67, 0x67, 0x53, 0, flags];
  // granule (q), serial (I), seq (I), crc (i), page_segments (B)
  parts.push(...i32(granule), ...i32(Math.floor(granule / 2 ** 32)));
  parts.push(...u32(serial), ...u32(seq), ...i32(0), segs.length);
  const out = new Uint8Array(parts.length + segs.length + packet.length);
  out.set(parts, 0);
  out.set(Uint8Array.from(segs), parts.length);
  out.set(packet, parts.length + segs.length);
  return out;
}

function identification(samplerate: number, channels = 2): Uint8Array {
  const parts = [0x01, 0x76, 0x6f, 0x72, 0x62, 0x69, 0x73]; // "\x01vorbis"
  parts.push(...u32(0)); // version
  parts.push(channels);
  parts.push(...i32(samplerate));
  parts.push(...i32(0), ...i32(128000), ...i32(0));
  parts.push(0xb8, 1);
  return Uint8Array.from(parts);
}

function encodeUtf8(text: string): number[] {
  return Array.from(new TextEncoder().encode(text));
}

function comment(entries: string[], vendor = "eudkit"): Uint8Array {
  const vendorBytes = encodeUtf8(vendor);
  const parts = [0x03, 0x76, 0x6f, 0x72, 0x62, 0x69, 0x73]; // "\x03vorbis"
  parts.push(...u32(vendorBytes.length), ...vendorBytes, ...u32(entries.length));
  for (const entry of entries) {
    const encoded = encodeUtf8(entry);
    parts.push(...u32(encoded.length), ...encoded);
  }
  parts.push(1);
  return Uint8Array.from(parts);
}

function stream(samplerate: number, granule: number, entries: string[] = [], channels = 2): Uint8Array {
  const serial = 0x1234;
  const parts: Uint8Array[] = [
    page(serial, 0, 0, identification(samplerate, channels), 0x02),
    page(serial, 1, 0, comment(entries), 0x00),
    page(serial, 2, granule, new Uint8Array(8), 0x04),
  ];
  return Buffer.concat(parts);
}

function fixture(name: string): string {
  return path.join(FIXTURES, "ogg", name);
}

function close(actual: number | null, expected: number, eps = 1e-12): void {
  assert.ok(actual !== null, `expected ${expected}, got null`);
  assert.ok(Math.abs(actual - expected) < eps, `expected ${expected}, got ${actual}`);
}

test("roundDuration rounds to three decimals", () => {
  assert.equal(roundDuration(1.2317687074829933), 1.232);
  assert.equal(roundDuration(1.0), 1.0);
  assert.equal(roundDuration(1.5), 1.5);
});

test("roundDuration ties round half away from zero", () => {
  // Shared rule with py/ogg.py; Python's round() would give 1.234 here.
  assert.equal(roundDuration(1.2345), 1.235);
  assert.equal(roundDuration(2.0005), 2.001);
});

test("known fixture durations", () => {
  const cases: Record<string, number> = {
    "one_second.ogg": 1.0,
    "one_and_half.ogg": 1.5,
    "two_seconds.ogg": 2.0,
  };
  for (const [name, expected] of Object.entries(cases)) {
    const info = readOgg(fixture(name));
    assert.equal(info.duration, expected, name);
    assert.equal(info.samplerate, 48000, name);
    assert.equal(roundDuration(info.duration as number), expected, name);
  }
});

test("odd length fixture uses the granule ratio", () => {
  const info = readOgg(fixture("odd_length.ogg"));
  assert.equal(info.samplerate, 44100);
  close(info.duration, 54321 / 44100);
  assert.equal(roundDuration(info.duration as number), 1.232);
});

test("identification header fields", () => {
  const info = readOgg(fixture("one_second.ogg"));
  // tinytag maps the nominal bitrate to kibibit/s and records the page start
  // as the audio offset.
  assert.equal(info.bitrate, 125.0);
  assert.equal(info.audio_offset, 0);
  // tinytag never assigns the vorbis channel count (source parity).
  assert.equal(info.channels, null);
});

test("parse bytes and read file agree", () => {
  const data = fs.readFileSync(fixture("one_and_half.ogg"));
  assert.equal(parseOgg(data).duration, 1.5);
  assert.equal(oggDuration(fixture("one_and_half.ogg")), 1.5);
});

test("vorbis comment fields", () => {
  const data = stream(48000, 48000, [
    "TITLE=Loop Theme",
    "artist=Someone",
    "date=2026",
    "tracknumber=07/12",
    "discnumber=1/2",
    "genre=Game",
    "description=notes",
    "album=Album",
    "albumartist=Various",
  ]);
  const info = parseOgg(data);
  assert.equal(info.title, "Loop Theme");
  assert.equal(info.artist, "Someone");
  assert.equal(info.year, "2026");
  assert.equal(info.track, "07");
  assert.equal(info.track_total, "12");
  assert.equal(info.disc, "1");
  assert.equal(info.disc_total, "2");
  assert.equal(info.genre, "Game");
  assert.equal(info.comment, "notes");
  assert.equal(info.album, "Album");
  assert.equal(info.albumartist, "Various");
});

test("unknown keys are ignored and the first value wins", () => {
  const data = stream(48000, 48000, ["TITLE=first", "TITLE=second", "custom=ignored", "=empty"]);
  const info = parseOgg(data);
  assert.equal(info.title, "first");
});

test("a non-utf8 comment value is skipped", () => {
  const vendor = Array.from(Buffer.from("eudkit"));
  const bad = [...Array.from(Buffer.from("TITLE=")), 0xff];
  const ok = Array.from(Buffer.from("ARTIST=ok"));
  const parts = [0x03, 0x76, 0x6f, 0x72, 0x62, 0x69, 0x73];
  parts.push(...u32(vendor.length), ...vendor, ...u32(2));
  parts.push(...u32(bad.length), ...bad);
  parts.push(...u32(ok.length), ...ok);
  parts.push(1);
  const packet = Uint8Array.from(parts);
  const data = Buffer.concat([
    page(1, 0, 0, identification(48000), 0x02),
    page(1, 1, 0, packet, 0x00),
    page(1, 2, 48000, new Uint8Array(8), 0x04),
  ]);
  const info = parseOgg(data);
  assert.equal(info.title, null);
  assert.equal(info.artist, "ok");
});

test("an empty file has no duration", () => {
  const tmpdir = fs.mkdtempSync(path.join(os.tmpdir(), "ogg-"));
  try {
    const empty = path.join(tmpdir, "empty.ogg");
    fs.writeFileSync(empty, new Uint8Array());
    const info = readOgg(empty);
    assert.ok(info instanceof OggInfo);
    assert.equal(info.duration, null);
  } finally {
    fs.rmSync(tmpdir, { recursive: true, force: true });
  }
});

test("a bad version is a format error", () => {
  const data = stream(48000, 48000);
  data[4] = 1; // page version must be 0
  assert.throws(
    () => parseOgg(data),
    (error: unknown) => {
      assert.ok(error instanceof OggError);
      assert.equal(error.code, "EUDSL-FORMAT");
      assert.match(error.message, /not a valid ogg file/);
      return true;
    },
  );
});

test("a truncated page header is a format error", () => {
  const data = Buffer.concat([stream(48000, 48000), Buffer.from("OggS\x00")]);
  assert.throws(() => parseOgg(data), /truncated ogg page header/);
});

test("a missing file is a format error", () => {
  assert.throws(() => readOgg(path.join(FIXTURES, "ogg", "nope.ogg")), /cannot read/);
});

test("non-ogg data is a format error", () => {
  // The reader starts at offset 0 (it never hunts for a later "OggS"), so a
  // page whose first bytes are not the capture pattern is rejected.
  assert.throws(() => parseOgg(Buffer.from("not an ogg stream" + "\x00".repeat(20))), /not a valid ogg file/);
});

test("short non-ogg data is a truncated header", () => {
  assert.throws(() => parseOgg(Buffer.from("not an ogg stream")), /truncated ogg page header/);
});

test("a large file scan finds the last granule", () => {
  // One page per chunk keeps the file above MAX_PAGE_SIZE so the scan starts
  // at the tail window and still reports the final granule.
  const parts: Uint8Array[] = [page(7, 0, 0, identification(48000), 0x02)];
  const payload = new Uint8Array(300);
  for (let seq = 1; seq < 400; seq += 1) {
    parts.push(page(7, seq, seq * 480, payload, 0x00));
  }
  const data = Buffer.concat(parts);
  assert.ok(data.length > 65536);
  const info = parseOgg(data);
  assert.equal(info.duration, (399 * 480) / 48000);
});
