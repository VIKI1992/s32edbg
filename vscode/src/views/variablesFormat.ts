// Pure formatting/validation helpers for the Variables view -- no vscode
// imports, so vitest covers them directly.
import { VarRow } from "../backend/types";

/** Scalar kinds whose display swaps to raw hex under the view's hex toggle.
 * Pointers are always hex; enum/char/float/bool keep their typed rendering
 * (their hex rides in the tooltip). */
const HEX_TOGGLE_KINDS = new Set(["int", "uint", "other"]);

export function rowDescription(row: VarRow, hexMode: boolean): string {
  if (row.error) {
    return `⚠ ${row.error}`;
  }
  if (!row.value) {
    return "";
  }
  if (hexMode && HEX_TOGGLE_KINDS.has(row.kind)) {
    return row.value.hex;
  }
  return row.value.display;
}

/** Stable change-tracking key: section + full path, independent of handles. */
export function rowKey(section: string, path: string): string {
  return `${section}/${path}`;
}

const WATCH_PATH_RE =
  /^\*{0,3}[A-Za-z_$][A-Za-z0-9_$]*(?:(?:\.|->)[A-Za-z_$][A-Za-z0-9_$]*|\[(?:\d+|0x[0-9a-fA-F]+)\])*$/;

/** Client-side pre-validation of a watch path; the backend stays authority. */
export function validateWatchPath(text: string): string | null {
  const s = text.trim();
  if (!s) {
    return "empty path";
  }
  if (!WATCH_PATH_RE.test(s)) {
    return "expected something like counter, cfg.limits[2].max, *ptr or p->x";
  }
  return null;
}

export function isStaleHandleError(message: string): boolean {
  return message.startsWith("stale-handle:");
}

/** Rows the edit command accepts: scalar, sized 1/2/4, readable, addressed
 * (or register-located, which the backend limits to frame 0). */
export function isEditable(row: VarRow): boolean {
  if (row.error || !row.value || row.handle === null) {
    return false;
  }
  if (["struct", "union", "array"].includes(row.kind)) {
    return false;
  }
  if (row.size === null || ![1, 2, 4].includes(row.size)) {
    return false;
  }
  return row.address !== null || row.register !== null;
}
