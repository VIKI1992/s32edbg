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
