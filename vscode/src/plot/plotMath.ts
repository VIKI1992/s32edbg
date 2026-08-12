/** Pure geometry/data helpers for the scope panel -- kept free of the
 * webview so they are unit-testable. Times are absolute milliseconds. */

export interface Pt {
  t: number;
  v: number;
}

/** Index of the sample nearest in time; -1 for an empty series.
 * `samples` must be ascending by t (the stream appends in order). */
export function nearestIndex(samples: Pt[], t: number): number {
  if (samples.length === 0) {
    return -1;
  }
  let lo = 0;
  let hi = samples.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (samples[mid].t < t) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  if (lo > 0 && Math.abs(samples[lo - 1].t - t) <= Math.abs(samples[lo].t - t)) {
    return lo - 1;
  }
  return lo;
}

export function nearestSample(samples: Pt[], t: number): Pt | null {
  const i = nearestIndex(samples, t);
  return i < 0 ? null : samples[i];
}

export interface CursorDelta {
  a: Pt | null;
  b: Pt | null;
  dv: number | null;
}

/** Per-signal values at the two cursors and their difference. */
export function cursorDelta(samples: Pt[], tA: number, tB: number): CursorDelta {
  const a = nearestSample(samples, tA);
  const b = nearestSample(samples, tB);
  return { a, b, dv: a !== null && b !== null ? b.v - a.v : null };
}

export interface Range {
  min: number;
  max: number;
}

/** Scale [min, max] around an anchor value: factor > 1 zooms out, < 1 zooms
 * in. A non-finite or inverted result collapses back to the input range; a
 * vanishing span is floored so the mapping stays invertible. */
export function zoomRange(
  min: number, max: number, anchor: number, factor: number,
): Range {
  let lo = anchor - (anchor - min) * factor;
  let hi = anchor + (max - anchor) * factor;
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi - lo <= 0) {
    return { min, max };
  }
  if (hi - lo < 1e-9) {
    const mid = (lo + hi) / 2;
    lo = mid - 5e-10;
    hi = mid + 5e-10;
  }
  return { min: lo, max: hi };
}

export interface Column {
  x: number;
  min: number;
  max: number;
  last: number;
}

/** Per-pixel-column min/max envelope over [t0, t1] -- keeps the canvas
 * O(columns) at kHz sample rates without hiding spikes. Empty columns are
 * skipped (the polyline connects across them). */
export function decimate(
  samples: Pt[], t0: number, t1: number, columns: number,
): Column[] {
  if (columns <= 0 || t1 <= t0) {
    return [];
  }
  const out: Column[] = [];
  const width = t1 - t0;
  let current: Column | null = null;
  for (const s of samples) {
    if (s.t < t0 || s.t > t1) {
      continue;
    }
    const x = Math.min(columns - 1, Math.floor(((s.t - t0) / width) * columns));
    if (current === null || current.x !== x) {
      if (current !== null) {
        out.push(current);
      }
      current = { x, min: s.v, max: s.v, last: s.v };
    } else {
      if (s.v < current.min) {
        current.min = s.v;
      }
      if (s.v > current.max) {
        current.max = s.v;
      }
      current.last = s.v;
    }
  }
  if (current !== null) {
    out.push(current);
  }
  return out;
}
