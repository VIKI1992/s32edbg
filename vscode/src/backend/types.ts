// Mirrors of the NDJSON shapes s32edbg/machine.py emits (protocol 1).

export interface EnvelopeError {
  kind:
    | "usage"
    | "tool"
    | "refused"
    | "aborted"
    | "internal"
    | "unknown-command";
  message: string;
}

export interface Envelope {
  seq: number;
  ok: boolean;
  cmd: string;
  result: unknown;
  error?: EnvelopeError;
  /** Backend wall time for the command, milliseconds. */
  ms?: number;
  /** DP/AP transactions the command issued -- each is one USB round trip. */
  ops?: number;
}

export interface ReadyEvent {
  event: "ready";
  version: string;
  protocol: number;
  cores: string[];
  m33: string[];
  focus: string | null;
  m33_focus: string | null;
  discovery_error: string | null;
}

export interface ProgressEvent {
  event: "progress";
  seq: number;
  message: string;
}

export interface ByeEvent {
  event: "bye";
  restore_notes: string[];
  kept: boolean;
}

/** One `var sample` streaming batch: raw memory reads, ~20 events/s.
 * `dt` are millisecond offsets from `t0_ms`, stamped per read backend-side. */
export interface SamplesEvent {
  event: "samples";
  t0_ms: number;
  signals: { name: string; dt: number[]; v: number[] }[];
}

/** The stream gave up (five consecutive failing batches). */
export interface SampleStoppedEvent {
  event: "sample-stopped";
  reason: string;
}

/** Pushed by the backend's halt monitor the moment a session-resumed core
 * halts on its own (breakpoint/watchpoint). Arrives between envelopes. */
export interface StoppedEvent {
  event: "stopped";
  family: "r52" | "m33";
  core: string;
  status?: string;
  location?: { known?: boolean; address?: string; source?: string };
}

export interface R52Row {
  core: string;
  halted: boolean | null;
  status?: string | null;
  in_reset?: boolean | null;
  os_locked?: boolean | null;
  unreachable?: string | null;
  location?: { known?: boolean; address?: string; source?: string };
}

export interface M33Row {
  name: string;
  halted: boolean | null;
  answered: boolean;
}

export interface PollResult {
  r52: R52Row[];
  m33: M33Row[];
  focus: string | null;
  m33_focus: string | null;
  sync: boolean;
  catch_armed: string[];
  discovery_error?: string;
}

export interface StackFrame {
  index: number;
  pc: string;
  sp: string | null;
  fp: string | null;
  via: string;
  func: string | null;
  file: string | null;
  line: number | null;
}

export interface DisasmRow {
  addr: string;
  size: number;
  bytes: string;
  mnemonic: string;
  op_str: string;
  func?: string;
  file?: string;
  line?: number;
}

export interface VarValue {
  display: string;
  hex: string;
}

export interface VarRow {
  handle: string | null;
  name: string;
  path: string;
  type: string;
  kind: string;
  value: VarValue | null;
  address: string | null;
  register: string | null;
  size: number | null;
  expandable: boolean;
  count: number | null;
  static: boolean;
  error: string | null;
}

export interface VarLocalsResult {
  core: string;
  frame: number;
  func: string | null;
  frame_pc: string;
  cfa: string | null;
  cfa_source: string | null;
  generation: number;
  variables: VarRow[];
  note: string | null;
}

export interface VarGlobalsResult {
  core: string;
  generation: number;
  total: number;
  shown: number;
  variables: VarRow[];
}

export interface VarExpandResult {
  generation: number;
  parent: string;
  children: VarRow[];
  more_from: number | null;
  note: string | null;
}

export interface VarWatchResult {
  generation: number;
  row: VarRow;
}

export interface VarWriteResult {
  wrote: string;
  readback: string;
  match: boolean;
  row: VarRow;
}

export interface VarRefreshResult {
  generation: number;
  rows: VarRow[];
}

export function hexToInt(text: string | null | undefined): number | null {
  if (!text) {
    return null;
  }
  const v = Number.parseInt(text, 16);
  return Number.isNaN(v) ? null : v;
}

export function toHex(value: number): string {
  return "0x" + value.toString(16).toUpperCase().padStart(8, "0");
}
