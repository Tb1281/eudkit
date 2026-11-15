/**
 * Build-time Ogg length reader (port of tinytag's `_TinyTag` / `_Ogg`).
 *
 * euddraft's `lib/soundlooper.py` carries a vendored copy of tinytag (MIT, Tom
 * Wallroth, 2014-2018) so it can measure each `.ogg` segment before embedding
 * it. eudkit has no plugin system, so that reader becomes a regular build-time
 * utility shared by the sound looper port and anyone else who needs the
 * duration of a Vorbis/Opus file.
 *
 * The port keeps tinytag's page walk verbatim: the duration is the largest page
 * granule position divided by the stream's sample rate. Only the container
 * parsing (page headers, packet lacing, the identification and comment
 * packets) is needed, so the reader never decodes audio and never verifies
 * CRCs.
 *
 * Public surface
 * --------------
 * `readOgg(path)` reads a file from disk; `parseOgg(data)` parses bytes that are
 * already in memory. Both return an `OggInfo` with tinytag's metadata fields
 * plus `duration` (seconds, `null` when the file carries no audio page).
 * `oggDuration(path)` is the one-call convenience form.
 *
 * Deviations from tinytag
 * -----------------------
 * - A file object is replaced by an in-memory cursor over the bytes, so the
 *   reader has no dependency on `BufferedReader.peek`/`seek` quirks. The
 *   byte-by-byte tail scan, the packet lacing rules, and the metadata mapping
 *   are unchanged.
 * - Truncated or malformed structures throw `OggError` (`EUDSL-FORMAT`).
 * - `roundDuration` rounds half away from zero to 3 decimals, shared verbatim
 *   with `py/ogg.py` so the Python and TypeScript frontends produce the same
 *   segment length. `Math.round` is half-up for positives, but naming the rule
 *   here keeps the two frontends pinned to one implementation.
 * - The comment keys are decoded exactly as tinytag decodes them; a key/value
 *   pair that is not valid UTF-8 is skipped rather than failing the read.
 */

import fs from "node:fs";

import { EudslError } from "./view.ts";

/** The largest Ogg page (https://xiph.org/ogg/doc/libogg/ogg_page.html). */
export const MAX_PAGE_SIZE = 65536;

const PAGE_HEADER_SIZE = 27;

/** Vorbis comment key -> `OggInfo` field (tinytag's mapping). */
const COMMENT_FIELDS: Record<string, keyof OggInfo> = {
  album: "album",
  albumartist: "albumartist",
  title: "title",
  artist: "artist",
  date: "year",
  tracknumber: "track",
  discnumber: "disc",
  genre: "genre",
  description: "comment",
};

/** Malformed or unreadable Ogg container (`EUDSL-FORMAT`). */
export class OggError extends EudslError {
  constructor(message: string) {
    super("EUDSL-FORMAT", `ogg: ${message}`);
  }
}

/** Round `seconds` to 3 decimals, half away from zero. */
export function roundDuration(seconds: number): number {
  return Math.floor(seconds * 1000 + 0.5) / 1000;
}

/** Minimal `read`/`peek`/`tell`/`seek` cursor over bytes. */
class Cursor {
  readonly data: Uint8Array;
  pos = 0;

  constructor(data: Uint8Array) {
    this.data = data;
  }

  read(count: number): Uint8Array {
    const start = this.pos;
    const end = Math.min(this.data.length, start + Math.max(0, count));
    this.pos = end;
    return this.data.subarray(start, end);
  }

  peek(count: number): Uint8Array {
    return this.data.subarray(
      this.pos,
      Math.min(this.data.length, this.pos + Math.max(0, count)),
    );
  }

  tell(): number {
    return this.pos;
  }

  seek(offset: number, whence: number = 0): void {
    let pos: number;
    if (whence === 0) pos = offset;
    else if (whence === 1) pos = this.pos + offset;
    else if (whence === 2) pos = this.data.length + offset;
    else throw new OggError(`unknown seek mode ${whence}`);
    this.pos = Math.min(Math.max(pos, 0), this.data.length);
  }
}

/** Cursor over one packet payload (tinytag's per-packet `BytesIO`). */
class PacketCursor {
  readonly data: Uint8Array;
  pos = 0;

  constructor(data: Uint8Array) {
    this.data = data;
  }

  read(count: number): Uint8Array {
    const start = this.pos;
    const end = Math.min(this.data.length, start + Math.max(0, count));
    this.pos = end;
    return this.data.subarray(start, end);
  }

  seek(offset: number, whence: number = 0): void {
    let pos: number;
    if (whence === 0) pos = offset;
    else if (whence === 1) pos = this.pos + offset;
    else if (whence === 2) pos = this.data.length + offset;
    else throw new OggError(`unknown seek mode ${whence}`);
    this.pos = Math.min(Math.max(pos, 0), this.data.length);
  }
}

function u32(view: DataView, offset: number): number {
  return view.getUint32(offset, true);
}

function u32From(cursor: PacketCursor, what: string): number {
  const raw = cursor.read(4);
  if (raw.length !== 4) throw new OggError(`truncated ${what}`);
  return new DataView(raw.buffer, raw.byteOffset, raw.byteLength).getUint32(
    0,
    true,
  );
}

function decodeUtf8(bytes: Uint8Array): string | null {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

function equalsAscii(bytes: Uint8Array, prefix: string): boolean {
  if (bytes.length < prefix.length) return false;
  for (let i = 0; i < prefix.length; i += 1) {
    if (bytes[i] !== prefix.charCodeAt(i)) return false;
  }
  return true;
}

/** Metadata read from an Ogg container (tinytag's field names). */
export class OggInfo {
  album: string | null = null;
  albumartist: string | null = null;
  artist: string | null = null;
  audio_offset: number | null = null;
  bitrate: number | null = null;
  channels: number | null = null;
  comment: string | null = null;
  disc: string | null = null;
  disc_total: string | null = null;
  duration: number | null = null;
  genre: string | null = null;
  samplerate: number | null = null;
  title: string | null = null;
  track: string | null = null;
  track_total: string | null = null;
  year: string | null = null;

  /** Store a comment value, splitting `track`/`disc` totals. */
  _setField(fieldname: "album" | "albumartist" | "artist" | "comment" | "disc" | "genre" | "title" | "track" | "year", value: string): void {
    if (this[fieldname]) return; // do not overwrite existing data
    if (fieldname === "track" || fieldname === "disc") {
      if (value.includes("/")) {
        const [current, total] = value.split("/");
        (this as unknown as Record<string, string>)[`${fieldname}_total`] = total;
        (this as unknown as Record<string, string>)[fieldname] = current;
      } else {
        (this as unknown as Record<string, string>)[fieldname] = value;
      }
      return;
    }
    (this as unknown as Record<string, string>)[fieldname] = value;
  }
}

class OggReader extends OggInfo {
  private readonly cursor: Cursor;
  readonly filesize: number;
  private tagsParsed = false;
  private maxSamplenum = 0; // maximum sample position ever read

  constructor(cursor: Cursor, filesize: number) {
    super();
    this.cursor = cursor;
    this.filesize = filesize;
  }

  load(): void {
    this.determineDuration(this.cursor);
  }

  private determineDuration(fh: Cursor): void {
    if (!this.tagsParsed) {
      this.parseTag(fh); // determine sample rate
      this.tagsParsed = true;
      fh.seek(0); // and rewind to start
    }
    if (this.filesize > MAX_PAGE_SIZE) {
      fh.seek(-MAX_PAGE_SIZE, 2); // last possible page position
    }
    for (;;) {
      const b = fh.peek(4);
      if (b.length === 0) return; // EOF
      if (equalsAscii(b, "OggS")) {
        // look for an ogg header
        for (const _packet of this.parsePages(fh)) {
          // parse all remaining pages
        }
        if (this.samplerate === null) {
          throw new OggError("audio stream has no sample rate");
        }
        this.duration = this.maxSamplenum / this.samplerate;
        return;
      }
      let idx = -1;
      for (let i = 0; i + 4 <= b.length; i += 1) {
        if (b[i] === 0x4f && b[i + 1] === 0x67 && b[i + 2] === 0x67 && b[i + 3] === 0x53) {
          idx = i;
          break;
        }
      }
      const seekpos = idx !== -1 ? idx : b.length - 3;
      fh.seek(Math.max(seekpos, 1), 1);
    }
  }

  private parseTag(fh: Cursor): void {
    let pageStartPos = fh.tell(); // set audio_offset later if audio data
    for (const packet of this.parsePages(fh)) {
      const walker = new PacketCursor(packet);
      if (equalsAscii(packet, "\x01vorbis")) {
        if (packet.length < 28) {
          throw new OggError("truncated vorbis identification packet");
        }
        const view = new DataView(
          packet.buffer,
          packet.byteOffset,
          packet.byteLength,
        );
        this.samplerate = view.getInt32(12, true);
        const bitrate = view.getInt32(20, true);
        if (!this.audio_offset) {
          this.bitrate = bitrate / 1024;
          this.audio_offset = pageStartPos;
        }
      } else if (equalsAscii(packet, "\x03vorbis")) {
        walker.seek(7); // jump over header name
        this.parseVorbisComment(walker);
      } else if (equalsAscii(packet, "OpusHead")) {
        // parse opus header
        walker.seek(8); // jump over header name
        const head = walker.read(11);
        if (head.length < 11) throw new OggError("truncated opus header");
        const view = new DataView(head.buffer, head.byteOffset, head.byteLength);
        const version = view.getUint8(0);
        const channels = view.getUint8(1);
        const sr = view.getUint32(4, true);
        if ((version & 0xf0) === 0) {
          // only major version 0 supported
          this.channels = channels;
          this.samplerate = sr;
        }
      } else if (equalsAscii(packet, "OpusTags")) {
        // parse opus metadata
        walker.seek(8);
        this.parseVorbisComment(walker);
      } else {
        break;
      }
      pageStartPos = fh.tell();
    }
  }

  private parseVorbisComment(fh: PacketCursor): void {
    // For the spec, see: http://xiph.org/vorbis/doc/v-comment.html
    const vendorLength = u32From(fh, "vorbis vendor length");
    fh.seek(vendorLength, 1); // jump over vendor
    const elements = u32From(fh, "vorbis comment count");
    for (let i = 0; i < elements; i += 1) {
      const length = u32From(fh, "vorbis comment length");
      const raw = fh.read(length);
      if (raw.length !== length) throw new OggError("truncated vorbis comment");
      const keyvalpair = decodeUtf8(raw);
      if (keyvalpair === null) continue;
      const equals = keyvalpair.indexOf("=");
      if (equals === -1) continue;
      const key = keyvalpair.slice(0, equals);
      const value = keyvalpair.slice(equals + 1);
      const fieldname = COMMENT_FIELDS[key.toLowerCase()];
      if (fieldname) {
        this._setField(
          fieldname as
            | "album"
            | "albumartist"
            | "artist"
            | "comment"
            | "disc"
            | "genre"
            | "title"
            | "track"
            | "year",
          value,
        );
      }
    }
  }

  private *parsePages(fh: Cursor): Generator<Uint8Array> {
    // For the spec, see: https://wiki.xiph.org/Ogg
    let previousPage: Uint8Array = new Uint8Array(0); // data carried over from continuations
    let headerData = fh.read(PAGE_HEADER_SIZE); // read an ogg page header
    while (headerData.length !== 0) {
      if (headerData.length !== PAGE_HEADER_SIZE) {
        throw new OggError("truncated ogg page header");
      }
      const view = new DataView(
        headerData.buffer,
        headerData.byteOffset,
        headerData.byteLength,
      );
      const oggs = u32(view, 0);
      const version = view.getUint8(4);
      const pos = view.getBigInt64(6, true);
      const segments = view.getUint8(26);
      const sampleNum = Number(pos);
      this.maxSamplenum = Math.max(this.maxSamplenum, sampleNum);
      if (oggs !== 0x5367674f || version !== 0) {
        throw new OggError("not a valid ogg file");
      }
      const rawSegs = fh.read(segments);
      if (rawSegs.length !== segments) {
        throw new OggError("truncated ogg segment table");
      }
      let total = 0;
      for (let i = 0; i < segments; i += 1) {
        // read all segments
        total += rawSegs[i];
        if (total < 255) {
          // less than 255 bytes means end of packet
          const payload = fh.read(total);
          if (payload.length !== total) throw new OggError("truncated ogg packet");
          yield concat(previousPage, payload);
          previousPage = new Uint8Array(0);
          total = 0;
        }
      }
      if (total !== 0) {
        const payload = fh.read(total);
        if (payload.length !== total) throw new OggError("truncated ogg packet");
        if (total % 255 === 0) {
          previousPage = concat(previousPage, payload);
        } else {
          yield concat(previousPage, payload);
          previousPage = new Uint8Array(0);
        }
      }
      headerData = fh.read(PAGE_HEADER_SIZE);
    }
  }
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array {
  if (a.length === 0) return b;
  if (b.length === 0) return a;
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

/** Read metadata (including `duration`) from in-memory Ogg bytes. */
export function parseOgg(data: Uint8Array): OggInfo {
  if (!(data instanceof Uint8Array)) {
    throw new OggError(
      `expected bytes, got ${typeof data === "object" ? "non-bytes" : typeof data}`,
    );
  }
  if (data.length === 0) return new OggInfo();
  const reader = new OggReader(new Cursor(data), data.length);
  reader.load();
  return reader;
}

/** Read metadata (including `duration`) from `path`. */
export function readOgg(path: string): OggInfo {
  if (typeof path !== "string" || path.length === 0) {
    throw new OggError("a non-empty path is required");
  }
  let data: Buffer;
  try {
    data = fs.readFileSync(path);
  } catch (error) {
    throw new OggError(`cannot read ${JSON.stringify(path)}: ${String(error)}`);
  }
  if (data.length === 0) return new OggInfo();
  return parseOgg(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
}

/** The stream duration in seconds, or `null` for an empty file. */
export function oggDuration(path: string): number | null {
  return readOgg(path).duration;
}

export const ogg_duration = oggDuration;
export const read_ogg = readOgg;
export const parse_ogg = parseOgg;
export const round_duration = roundDuration;
