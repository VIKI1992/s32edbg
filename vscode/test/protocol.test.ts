import { describe, expect, it } from "vitest";
import { hexToInt, toHex } from "../src/backend/types";

describe("hex conversions (the backend speaks 0x strings)", () => {
  it("round-trips", () => {
    expect(hexToInt("0x32100020")).toBe(0x32100020);
    expect(toHex(0x32100020)).toBe("0x32100020".toUpperCase().replace("0X", "0x"));
    expect(toHex(0x1)).toBe("0x00000001");
  });
  it("rejects junk", () => {
    expect(hexToInt(null)).toBeNull();
    expect(hexToInt(undefined)).toBeNull();
    expect(hexToInt("")).toBeNull();
    expect(hexToInt("zz")).toBeNull();
  });
});

describe("NDJSON line framing", () => {
  // The same split/parse loop SessionClient runs on stdout chunks.
  function frame(chunks: string[]): unknown[] {
    let buf = "";
    const out: unknown[] = [];
    for (const chunk of chunks) {
      buf += chunk;
      let idx: number;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (line) {
          out.push(JSON.parse(line));
        }
      }
    }
    return out;
  }

  it("reassembles envelopes split across chunks", () => {
    const objs = frame([
      '{"seq":1,"ok":tr',
      'ue,"cmd":"core halt","result":null}\n{"seq":2,',
      '"ok":false,"cmd":"x","error":{"kind":"usage","message":"m"}}\n',
    ]);
    expect(objs).toHaveLength(2);
    expect((objs[0] as { seq: number }).seq).toBe(1);
    expect((objs[1] as { error: { kind: string } }).error.kind).toBe("usage");
  });

  it("ignores blank lines and keeps partial tails", () => {
    const objs = frame(['\n\n{"event":"ready","protocol":1}\n{"seq":1']);
    expect(objs).toHaveLength(1);
  });
});
