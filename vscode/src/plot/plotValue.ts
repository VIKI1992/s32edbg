import { VarRow } from "../backend/types";

/** Decode one raw unsigned word into a plottable number by type metadata.
 *
 * This is what the streaming path uses: the backend sends the words it
 * read, unsigned; signedness, floats and bools are the frontend's problem.
 * Signed ints get two's-complement correction by size, float32 is bit-cast,
 * bools become 0/1 so a toggle draws as a square wave; everything else
 * (uint, enum, char, pointer) is the raw magnitude.
 */
export function decodeRaw(
  kind: string, size: number | null, raw: number,
): number | null {
  if (!Number.isFinite(raw)) {
    return null;
  }
  const k = kind.toLowerCase();
  if (k === "float") {
    if ((size ?? 4) === 4) {
      const view = new DataView(new ArrayBuffer(4));
      view.setUint32(0, raw >>> 0);
      return view.getFloat32(0);
    }
    return null;
  }
  if (k === "bool") {
    return raw === 0 ? 0 : 1;
  }
  if (k === "int") {
    const bits = (size ?? 4) * 8;
    if (bits > 0 && bits <= 32) {
      const limit = 2 ** bits;
      return raw >= limit / 2 ? raw - limit : raw;
    }
  }
  return raw;
}

/** Decode one fetched row (`value.hex` machine field), or null when the tick
 * legitimately produced no sample (error row, no value, unparseable).
 * Floats prefer the backend's display text (already decoded) over bit-cast.
 */
export function decodeSample(row: VarRow | undefined): number | null {
  if (!row || row.error || !row.value) {
    return null;
  }
  const raw = Number.parseInt(row.value.hex.replace(/^0x/i, ""), 16);
  if (row.kind.toLowerCase() === "float") {
    const fromDisplay = Number.parseFloat(row.value.display);
    if (!Number.isNaN(fromDisplay)) {
      return fromDisplay;
    }
  }
  if (Number.isNaN(raw)) {
    return null;
  }
  return decodeRaw(row.kind, row.size, raw);
}
