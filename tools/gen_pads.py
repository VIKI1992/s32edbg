"""Generate s32edbg/data/pads_bga27.py from NXP's S32E2 IOMUX attachment.

The ball -> SIUL2-pad mapping lives in ``S32E2_IOMUX.xlsx``, which NXP ships
as a file attachment *embedded inside* the S32E2 Reference Manual PDF (the RM
text: "See the IOMUX file attached to this document"). This script accepts
either the extracted .xlsx or the RM .pdf itself (extraction needs pypdf; the
xlsx parse is stdlib-only so the generated table never depends on anything).

Two sheets matter:

  * "IO Signal Table"    -- main die: Port=PAD_xxx, CR=MSCR index,
                            CR Instance Name=SIUL2_0/SIUL2_1, BGA975=ball.
  * "AE IO Signal Table" -- AE die: Port=PBx_nn (the pad IS the port name),
                            CR=MSCR index in SIUL2_AE, BGA975=ball.

Each pad spans several rows (one per mux function); only the header row of a
pad carries the ball, which is exactly the row this script keeps.

Usage:
    python tools/gen_pads.py --from "<S32E2_IOMUX.xlsx or S32E27RM.pdf>"
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

OUT = Path(__file__).resolve().parent.parent / "s32edbg" / "data" / "pads_bga27.py"
ATTACHMENT = "S32E2_IOMUX.xlsx"
BALL_RE = re.compile(r"^[A-Z]{1,2}[0-9]{1,2}$")


def load_xlsx_bytes(source: Path) -> bytes:
    if source.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            sys.exit("extracting from a PDF needs pypdf: pip install pypdf "
                     "(or extract the attachment manually and pass the .xlsx)")
        reader = PdfReader(str(source))
        attachments = reader.attachments
        if ATTACHMENT not in attachments:
            sys.exit(f"{source} has no embedded {ATTACHMENT}; found: "
                     f"{sorted(attachments)}")
        blob = attachments[ATTACHMENT]
        return blob[0] if isinstance(blob, list) else blob
    return source.read_bytes()


def sheet_rows(z: zipfile.ZipFile, target: str) -> list[dict[int, str]]:
    """Rows of one worksheet as {column index: text}, shared strings resolved."""
    shared: list[str] = []
    try:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{M}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{M}t")))
    except KeyError:
        pass
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid2file = {rel.get("Id"): rel.get("Target") for rel in rels}
    name2file = {
        sh.get("name"): "xl/" + rid2file[sh.get(f"{R}id")].lstrip("/")
        for sh in wb.iter(f"{M}sheet")
    }
    if target not in name2file:
        sys.exit(f"sheet {target!r} not in workbook (has: {sorted(name2file)})")

    def col_of(ref: str) -> int:
        n = 0
        for ch in re.match(r"[A-Z]+", ref).group(0):
            n = n * 26 + ord(ch) - 64
        return n - 1

    rows: list[dict[int, str]] = []
    tree = ET.fromstring(z.read(name2file[target]))
    for row in tree.find(f"{M}sheetData").findall(f"{M}row"):
        vals: dict[int, str] = {}
        for c in row.findall(f"{M}c"):
            v = c.find(f"{M}v")
            if v is None or v.text is None:
                continue
            text = shared[int(v.text)] if c.get("t") == "s" else v.text
            if text:
                vals[col_of(c.get("r"))] = text
        rows.append(vals)
    return rows


def header_columns(rows: list[dict[int, str]], *needed: str) -> tuple[int, dict[str, int]]:
    """(header row index, column name -> index) for the row containing 'Port'."""
    for i, row in enumerate(rows):
        names = {v.strip(): k for k, v in row.items()}
        if all(n in names for n in needed):
            return i, {n: names[n] for n in needed}
    sys.exit(f"no header row carrying {needed}")


def collect(source: Path) -> dict[str, tuple[str, str, int]]:
    z = zipfile.ZipFile(io.BytesIO(load_xlsx_bytes(source)))
    out: dict[str, tuple[str, str, int]] = {}

    rows = sheet_rows(z, "IO Signal Table")
    hdr, cols = header_columns(rows, "Port", "CR", "CR Instance Name", "BGA975")
    for row in rows[hdr + 1 :]:
        pad = row.get(cols["Port"], "").strip()
        ball = row.get(cols["BGA975"], "").strip()
        if not pad.startswith("PAD_") or not BALL_RE.match(ball):
            continue
        entry = (pad, row.get(cols["CR Instance Name"], "?").strip(),
                 int(row.get(cols["CR"], "-1")))
        if ball in out and out[ball] != entry:
            sys.exit(f"ball {ball} mapped twice: {out[ball]} vs {entry}")
        out[ball] = entry

    rows = sheet_rows(z, "AE IO Signal Table")
    hdr, cols = header_columns(rows, "Port", "CR", "BGA975")
    for row in rows[hdr + 1 :]:
        pad = row.get(cols["Port"], "").strip()
        ball = row.get(cols["BGA975"], "").strip()
        if not pad or not BALL_RE.match(ball):
            continue
        entry = (pad, "SIUL2_AE", int(row.get(cols["CR"], "-1")))
        if ball in out and out[ball] != entry:
            sys.exit(f"ball {ball} mapped twice: {out[ball]} vs {entry}")
        out[ball] = entry
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="source", required=True,
                    help="S32E2_IOMUX.xlsx, or the RM PDF carrying it as an attachment")
    args = ap.parse_args()
    source = Path(args.source)
    table = collect(source)
    main_n = sum(1 for v in table.values() if v[1] != "SIUL2_AE")
    ae_n = len(table) - main_n

    lines = [
        '"""BGA 27x27 ball -> SIUL2 pad identity. GENERATED -- do not edit.',
        "",
        f"Source: {ATTACHMENT} (sheets 'IO Signal Table' + 'AE IO Signal Table'),",
        f"as embedded in {source.name}, extracted {_dt.date.today().isoformat()}.",
        f"{main_n} main-die pads (SIUL2_0/SIUL2_1, PAD_xxx) + {ae_n} AE-die pads",
        "(SIUL2_AE, where the pad name IS the port name, e.g. PBA_00).",
        "Regenerate with:  python tools/gen_pads.py --from <IOMUX xlsx or RM pdf>",
        '"""',
        "",
        "#: ball -> (pad name, SIUL2 instance, MSCR/CR index)",
        "BALL_TO_PAD: dict[str, tuple[str, str, int]] = {",
    ]
    for ball in sorted(table):
        pad, inst, cr = table[ball]
        lines.append(f'    "{ball}": ("{pad}", "{inst}", {cr}),')
    lines += ["}", ""]
    OUT.write_text("\n".join(lines), encoding="ascii")
    print(f"wrote {OUT}: {len(table)} balls ({main_n} main + {ae_n} AE)")


if __name__ == "__main__":
    main()
