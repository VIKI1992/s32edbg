import { VarRow } from "../backend/types";

/** Decode one sampled row into a plottable number, or null when the tick
 * legitimately produced no sample (error row, no value, unparseable).
 *
 * `value.hex` is the machine-readable field -- the raw word(s) the backend
 * read -- while `display` is type-formatted text. Integers come out of hex
 * with two's-complement correction by size; floats prefer the display text
 * (already decoded by the backend) and fall back to bit-casting the raw
 * word; bools become 0/1 so a toggle draws as a square wave.
 */
export function decodeSample(row: VarRow | undefined): number | null {
  if (!row || row.error || !row.value) {
    return null;
  }
  const hexText = row.value.hex.replace(/^0x/i, "");
  const raw = Number.parseInt(hexText, 16);
  const kind = row.kind.toLowerCase();

  if (kind === "float") {
    const fromDisplay = Number.parseFloat(row.value.display);
    if (!Number.isNaN(fromDisplay)) {
      return fromDisplay;
    }
    if (!Number.isNaN(raw) && (row.size ?? 4) === 4) {
      const view = new DataView(new ArrayBuffer(4));
      view.setUint32(0, raw >>> 0);
      return view.getFloat32(0);
    }
    return null;
  }
  if (Number.isNaN(raw)) {
    return null;
  }
  if (kind === "bool") {
    return raw === 0 ? 0 : 1;
  }
  if (kind === "int") {
    const bits = (row.size ?? 4) * 8;
    if (bits > 0 && bits <= 32) {
      const limit = 2 ** bits;
      return raw >= limit / 2 ? raw - limit : raw;
    }
  }
  // uint, enum, char, pointer and anything else: the raw magnitude.
  return raw;
}
