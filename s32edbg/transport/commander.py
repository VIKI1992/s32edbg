"""J-Link Commander transport: batch script runner and cross-check oracle.

Why batch and not interactive
-----------------------------
An interactive subprocess was tried first and does not work on Windows.
JLink.exe writes its "J-Link>" prompt with no trailing newline *and*
block-buffers stdout when it is not attached to a console, so the banner and
prompt never reach the parent process -- the pipe stays completely empty until
the process exits. Driving it line-by-line would need a ConPTY, which is not
worth it when ``-CommanderScript`` works perfectly.

Consequence for the interface
-----------------------------
State (notably the DP SELECT register) only persists *within one script run*, so
this transport cannot serve single DP/AP transactions one at a time. Those
methods raise with an explanation and point at the DLL transport. What it does
provide is:

  * ``probe_info()`` -- probe identity, VTREF and pin states, with no DAP access
  * ``run_script()`` -- an arbitrary command batch, returned as text
  * ``read_dap_batch()`` -- a whole DP/AP read sequence in one run, which is how
    DLL-transport results get independently corroborated

Every command used here was read out of the shipped JLink.exe help table; the
UM08001 in the source folder is a 36-page Getting Started booklet that documents
no Commander commands at all.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from ..errors import TransportError
from ..jtag import tapseq
from . import parsing
from .base import ProbeInfo, Tif, Transport
from .install import JLinkInstall, discover

DEFAULT_TIMEOUT = 90.0

#: Commands that block waiting for a device on stdin, which hangs a batch run.
#: Both prompt with "Please specify device / core." when no target is connected.
#:
#: JLink.exe's help gives the syntax as "MS <Scan chain>", so a form that
#: supplies the argument may well not prompt at all. That is worth one bench
#: experiment -- MS is the purpose-built scan-chain length measurement -- but
#: until someone runs it, blocking is the safe default: a prompt in batch mode
#: costs a full timeout.
INTERACTIVE_COMMANDS = ("IS", "MS")

#: WJTAGDR takes a Data64, so it cannot carry more than 64 bits in one shift.
MAX_WJTAGDR_BITS = 64


def _wjtagdr(value: int, nbits: int) -> str:
    return f"WJTAGDR 0x{value:X}, {nbits}"


def _wjtagraw(nbits: int, tms: int, tdi: int) -> str:
    """Format a raw vector. tapseq builds tms/tdi LSB-first, which is the order
    WJTAGRaw documents ("Write Raw JTAG data (LSB first)")."""
    return f"WJTAGRaw {nbits} {tms:X} {tdi:X}"


def _decode_tdo(hex_text: str, nbits: int, *, order: str = "little") -> int:
    """Turn a WJTAGRaw " TDO: <hex>" capture into an LSB-first integer.

    JLink.exe prints the capture with '%.2X' per whole byte and a '%.1X' tail
    nibble, i.e. a byte array rather than one wide integer. Bit 0 of the result
    must be the first bit clocked out, which is what tapseq.Sequence.extract()
    indexes against.

    ``order`` is which end of the printed string holds the first byte clocked.
    It is an assumption until calibrate_tdo_order() proves it on hardware --
    nothing here has ever run against this target.
    """
    raw = hex_text.strip()
    nbytes = (nbits + 7) // 8
    # A trailing '%.1X' nibble makes the string odd-length. It is the *low*
    # nibble of the final byte -- the remaining bits of a partial byte -- so the
    # pad goes in front of it, not after. Appending would multiply that byte
    # by 16 and corrupt the top of the capture.
    if len(raw) % 2:
        raw = raw[:-1] + "0" + raw[-1]
    data = bytes.fromhex(raw)
    data = data[:nbytes] if order == "little" else data[-nbytes:]
    return int.from_bytes(data, order) & ((1 << nbits) - 1)  # type: ignore[arg-type]


class CommanderTransport(Transport):
    """Batch-mode JLink.exe runner."""

    name = "commander"
    #: JLink.exe's own ReadAP help states the JTAG-DP pipeline explicitly:
    #: "First read returns data of the previous read. An additional read of DP
    #: reg 3 is necessary to get the data."
    ap_read_pipelined = True
    #: WJTAGRaw drives arbitrary-width vectors *and* prints the capture back as
    #: " TDO: <hex>", so this is the only backend that can read a register wider
    #: than WJTAGDR's 64-bit ceiling -- which the 208-bit RM Table 984 preamble
    #: and the boundary-scan register both are.
    can_raw_shift = True
    raw_shift_returns_tdo = True

    def __init__(
        self,
        install: JLinkInstall | None = None,
        *,
        serial: str | None = None,
        log_file: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        echo: bool = False,
        device: str | None = None,
        interface: Tif = Tif.JTAG,
        speed_khz: int = 4000,
    ) -> None:
        self.install = install or discover()
        self.serial = serial
        self.log_file = Path(log_file) if log_file else None
        self.timeout = timeout
        self.echo = echo
        self.device = device
        self.interface = interface
        self.speed_khz = speed_khz
        self.capability_warnings: list[str] = []
        self.last_output = ""
        #: Which end of WJTAGRaw's printed capture holds the first-clocked byte.
        #: Assumed until calibrate_tdo_order() proves it against hardware.
        self.tdo_byte_order = "little"

    # ---------------------------------------------------------- lifecycle --

    def open(self) -> None:
        if not self.install.commander.is_file():
            raise TransportError(f"JLink.exe not found at {self.install.commander}")

    def close(self) -> None:
        return None

    # ------------------------------------------------------------- batch ---

    def run_script(self, commands: list[str], *, add_quit: bool = True) -> str:
        """Run a command batch and return the whole transcript."""
        for cmd in commands:
            head = cmd.strip().split()[0].upper() if cmd.strip() else ""
            if head in INTERACTIVE_COMMANDS:
                raise TransportError(
                    f"{head} prompts for a device on stdin and would hang a batch run; "
                    "measure register lengths through the DLL transport instead"
                )

        lines = list(commands)
        if add_quit:
            lines.append("qc")
        script = "\n".join(lines) + "\n"

        with tempfile.TemporaryDirectory(prefix="s32edbg-") as tmp:
            path = Path(tmp) / "commands.jlink"
            path.write_text(script, encoding="ascii")
            argv = [str(self.install.commander), "-NoGui", "1"]
            if self.serial:
                argv += ["-SelectEmuBySN", self.serial]
            if self.log_file:
                argv += ["-Log", str(self.log_file)]
            argv += ["-CommanderScript", str(path)]

            if self.echo:
                for line in lines:
                    print(f"> {line}")
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=str(self.install.path),
                    encoding="utf-8",
                    errors="replace",
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as exc:
                raise TransportError(
                    f"JLink.exe did not finish within {self.timeout:.0f}s. A command may be "
                    "waiting for input; interactive commands are not usable in batch mode."
                ) from exc
            except OSError as exc:
                raise TransportError(f"could not run {self.install.commander}: {exc}") from exc

        out = proc.stdout + proc.stderr
        self.last_output = out
        if self.echo:
            print(out)
        for w in parsing.find_capability_warnings(out):
            if w not in self.capability_warnings:
                self.capability_warnings.append(w)
        return out

    def _bringup_commands(self) -> list[str]:
        """The sequence that actually brings this target's DAP up.

        ``JTAGConf -1 -1`` requests auto-detect (negative = auto), and a real
        ``connect`` is what makes J-Link scan the chain and initialise the DAP.
        Without the connect, every ReadDP returns 0.
        """
        cmds: list[str] = []
        if self.device:
            cmds.append(f"device {self.device}")
        cmds.append("si JTAG" if self.interface is Tif.JTAG else "si SWD")
        cmds.append(f"speed {self.speed_khz}")
        if self.interface is Tif.JTAG:
            cmds.append("JTAGConf -1 -1")
        cmds.append("connect")
        return cmds

    # -------------------------------------------------------------- probe --

    def probe_info(self) -> ProbeInfo:
        text = self.run_script(["ShowHWStatus", "ShowFWInfo"])
        fields = parsing.parse_probe_banner(text)
        info = ProbeInfo(
            serial=fields["serial"],
            hardware_version=fields["hardware_version"],
            firmware=fields["firmware"],
            dll_version=fields["dll_version"],
            vtref_mv=parsing.parse_vtref_mv(text),
            pins=parsing.parse_pins(text),
        )
        info.notes.append(f"J-Link install: {self.install.path} (v{self.install.version_str})")
        info.notes.append("batch mode: one JLink.exe run per operation")
        info.capabilities = list(self.capability_warnings)
        return info

    def select_interface(self, tif: Tif) -> None:
        self.interface = tif
        self.selected_tif = Tif(int(tif))

    def set_speed_khz(self, khz: int) -> None:
        self.speed_khz = khz

    # -------------------------------------------------------- DAP oracle ---

    def sample_activity(self, count: int = 5, delay_ms: int = 400) -> list[int]:
        """Sample target supply current repeatedly.

        A moving ITarget with a dead TAP says the silicon is executing and
        resetting rather than sitting broken, which is what separates a boot-loop
        or POR-hold from a wiring fault.
        """
        cmds: list[str] = []
        for i in range(count):
            cmds.append("st")
            if i != count - 1:
                cmds.append(f"Sleep {delay_ms}")
        text = self.run_script(cmds)
        import re

        return [int(m) for m in re.findall(r"ITarget\s*=\s*(\d+)\s*mA", text, re.IGNORECASE)]

    def scan_chain(self) -> dict[str, object]:
        """Scan the JTAG chain and report what J-Link found."""
        text = self.run_script(self._bringup_commands())
        result: dict[str, object] = {"raw": text}
        try:
            result["idcode"] = parsing.parse_jtag_id(text)
        except TransportError as exc:
            result["idcode"] = None
            result["error"] = str(exc)
        result["total_ir_len"] = parsing.parse_irlen(text)
        result["capability_warnings"] = list(self.capability_warnings)
        return result

    def read_dp_identity(self) -> dict[str, int | None]:
        """DPIDR, CTRL/STAT and DPIDR1 in one batch run.

        This backend cannot serve single DP reads, but a whole identity read
        fits inside one JLink.exe invocation -- which is exactly what the
        cross-transport check needs. Comparing this DPIDR against the dll
        backend's is what validates the hand-written ctypes signatures, since
        there is no SDK header to check them against.

        Each register is read twice and the second value taken. JLink.exe's own
        ReadDP help states the JTAG-DP pipeline: "JTAG: Data of the previous
        read is returned." A single read would report whatever the preceding
        transaction left behind.
        """
        cmds = self._bringup_commands()
        cmds += [
            "WriteDP 2 0x00000000",  # SELECT: DPBANKSEL 0
            "ReadDP 0",
            "ReadDP 0",  # DPIDR
            "ReadDP 1",
            "ReadDP 1",  # CTRL/STAT
            "WriteDP 2 0x00000001",  # SELECT: DPBANKSEL 1
            "ReadDP 0",
            "ReadDP 0",  # DPIDR1
        ]
        text = self.run_script(cmds)

        # Attribute each value to the command that produced it. A WriteDP echoes
        # a value too ("Writing DP register 2 = 0x..."), so counting positions
        # across the whole transcript pairs reads with writes -- which reported
        # a CTRL/STAT value as the DPIDR.
        reads = parsing.parse_command_values(text, "ReadDP")

        def settled(pair_index: int) -> int | None:
            """The second read of a pair, which is the one that has settled."""
            i = pair_index * 2 + 1
            return reads[i] if i < len(reads) else None

        # DPACC address 0b00 is *Reserved* on a JTAG-DP (ADIv6 Figure A1-2); the
        # DP identification lives on the separate IDCODE scan chain. So take the
        # DPIDR from J-Link's own connect banner, which reads it correctly, and
        # fall back to the DPACC value only if the banner is absent -- marking
        # it as untrustworthy would be better than silently reporting the
        # reserved register's contents as an identity.
        # DPIDR1 sits at the same DPACC address 0x0 with DPBANKSEL=1, so it is
        # unreachable over JTAG for the same reason and is reported as absent
        # rather than as the reserved register's contents.
        return {
            "dpidr": parsing.parse_dpidr(text),
            "ctrl_stat": settled(1),
            "dpidr1": None,
        }

    def attach_and_hold(self, sysrstc_offset: int, sysfuncreq: int) -> dict[str, object]:
        """Connect, power the DAP and hold the system, all in one invocation.

        This has to be a single run. J-Link's own connect resets the target, and
        the window that matters is between that reset and application code
        reaching the pad configuration -- RM 135.15.1 warns the debug pads
        "operate in JTAG functionality out of reset but can be later reassigned
        to their alternate functionalities". Spreading these steps across
        separate JLink.exe invocations puts a fresh reset, and a fresh boot,
        between each one, so the hold never lands.

        The write is SDASYSRSTC[SYSFUNCREQ] -- RM 135.11's "allows the debugger
        to hold the system in functional reset". RM Table 301 confirms a
        functional reset leaves the JTAGC and SWJ-DP running, so the connection
        survives the thing that stops the target.
        """
        from ..data.expected_apmap import AP_SDA

        full = AP_SDA + sysrstc_offset
        cmds = self._bringup_commands() + [
            # Request debug+system power and clear the sticky flags in the same
            # write, as Dap.power_up() does: a latched STICKYERR makes the DP
            # discard AP traffic while still acknowledging power.
            "WriteDP 1 0xF0000032",
            "ReadDP 1",
            "ReadDP 1",
            f"WriteDP 2 0x{full & 0xFFFF_FFF0:08X}",
            f"WriteAP {(full >> 2) & 3} 0x{sysfuncreq:08X}",
            f"WriteDP 2 0x{full & 0xFFFF_FFF0:08X}",
            f"ReadAP {(full >> 2) & 3}",
            "ReadDP 3",
        ]
        text = self.run_script(cmds)
        dp = parsing.parse_command_values(text, "ReadDP")
        return {
            "idcode": parsing.parse_jtag_id(text) if "Id:" in text else None,
            "dpidr": parsing.parse_dpidr(text),
            "ctrl_stat": dp[1] if len(dp) > 1 else None,
            "sysrstc_readback": dp[2] if len(dp) > 2 else None,
            "raw": text,
        }

    def read_dap_batch(self, reads: list[tuple[int, int]]) -> list[int | None]:
        """Read AP words in one batch, as an independent check on the DLL path.

        ``reads`` is a list of (ap_base, offset). Each becomes the ADIv6 sequence
        WriteDP 2 <select> / ReadAP <index> / ReadDP 3, which is the pattern the
        prior hardware recon used successfully.
        """
        cmds = self._bringup_commands()
        for ap_base, offset in reads:
            full = (ap_base + offset) & 0xFFFF_FFFF
            cmds.append(f"WriteDP 2 0x{full & 0xFFFF_FFF0:08X}")
            cmds.append(f"ReadAP {(full >> 2) & 3}")
            cmds.append("ReadDP 3")
        text = self.run_script(cmds)

        values: list[int | None] = []
        # The value of interest is the RDBUFF read that follows each ReadAP.
        for block in text.split("ReadDP 3")[1:]:
            first_line = block.strip().splitlines()[0] if block.strip() else ""
            try:
                values.append(parsing.parse_reg_value(first_line))
            except TransportError:
                values.append(None)
        while len(values) < len(reads):
            values.append(None)
        return values[: len(reads)]

    # ---------------------------------------------------------- raw JTAG ---
    #
    # Syntax, from JLink.exe's own embedded help table:
    #   WJTAGIR  <Instruction(hex)>, [<IRLen(dec)>]     (default IRLen=4)
    #   WJTAGDR  <Data64(hex)>, <NumBits(dec)>          prints "returns 0x..."
    #   WJTAGRaw <NumBits(dec)> <TMS(hex)> <TDI(hex)>   prints " TDO: <hex>"
    #   ResetTAP                                        "using state machine (111110)"
    #
    # WJTAGRaw is the primitive everything here is built on: arbitrary width,
    # explicit TMS per clock, and it reports the capture. WJTAGDR is kept only
    # as an independent oracle (cross_check_dr) because it tops out at 64 bits
    # and routes through J-Link's own chain model.
    #
    # None of these commands has ever been run against this target -- the 13
    # prior evidence directories only ever used ReadAP/WriteAP/ReadDP/WriteDP.
    # Treat this path as unvalidated until it runs on the bench, and see
    # calibrate_tdo_order() for the one assumption that must be settled first.

    def _run_raw(self, vectors: list[tuple[int, int, int]]) -> list[int]:
        """Clock raw vectors inside one JLink.exe run and return their captures.

        Everything raw goes through WJTAGRaw rather than WJTAGIR/WJTAGDR,
        because those two route through J-Link's own IRPre/IRPost chain model
        and would add an assumption about chain layout on top of the one being
        measured. WJTAGRaw drives TMS explicitly, so the vector tapseq built is
        exactly what reaches the pins.
        """
        cmds = self._bringup_commands() + [_wjtagraw(n, tms, tdi) for n, tms, tdi in vectors]
        text = self.run_script(cmds)
        captures = parsing.parse_tdo_captures(text)
        if len(captures) < len(vectors):
            err = parsing.find_error(text)
            raise TransportError(
                f"expected {len(vectors)} WJTAGRaw TDO capture(s) but parsed "
                f"{len(captures)}{f'; {err}' if err else ''}"
            )
        return [
            _decode_tdo(text_hex, nbits, order=self.tdo_byte_order)
            for text_hex, (nbits, _, _) in zip(captures[-len(vectors) :], vectors)
        ]

    def calibrate_tdo_order(self) -> str:
        """Settle which end of WJTAGRaw's printed capture is clocked first.

        Loads BYPASS and shifts an asymmetric pattern through it. BYPASS is a
        one-bit shift register (RM 136.3.2), so whatever goes in comes back
        delayed by exactly one clock -- an answer known in advance and asymmetric
        enough that the wrong byte order cannot produce it by chance.

        BYPASS is all-ones at either IR width (0xFF and 0xFFFF), so this needs no
        prior knowledge of the IR length. Sets and returns ``tdo_byte_order``.
        """
        pattern, nbits = 0x6A3D_1C97, 32
        # BYPASS captures 0 in Capture-DR and then delays TDI by exactly one
        # clock, so TDO bit k is TDI bit k-1. Shifting LSB-first that makes the
        # capture (pattern << 1), not (pattern >> 1) -- the direction is easy to
        # get backwards and doing so makes a working path look broken.
        expected = (pattern << 1) & ((1 << nbits) - 1)
        for order in ("little", "big"):
            self.tdo_byte_order = order
            try:
                ir = tapseq.ir_scan(0xFF, 8, from_reset=True)
                dr = tapseq.dr_scan(pattern, nbits, from_reset=False)
                captures = self._run_raw(
                    [(ir.nbits, ir.tms, ir.tdi), (dr.nbits, dr.tms, dr.tdi)]
                )
            except TransportError:
                continue
            if dr.extract(captures[1]) == expected:
                return order
        raise TransportError(
            "could not establish WJTAGRaw's TDO byte order: shifting a known pattern "
            "through BYPASS produced the expected one-bit delay under neither "
            "interpretation. The raw JTAG path cannot be trusted until this passes."
        )

    def raw_shift(self, nbits: int, tms: int, tdi: int) -> int | None:
        return self._run_raw([(nbits, tms, tdi)])[0]

    def reset_tap(self) -> None:
        # "Reset TAP Controller using state machine (111110)" -- which is
        # tapseq.TLR_TO_IDLE exactly.
        self.run_script(self._bringup_commands() + ["ResetTAP"])

    def ir_shift(self, value: int, nbits: int) -> int:
        """Load the instruction register.

        The instruction does not survive this call: the next one is a fresh
        JLink.exe run that reconnects and rescans the chain. Use jtag_sequence()
        whenever an instruction and its data register have to stay paired.
        """
        seq = tapseq.ir_scan(value, nbits, from_reset=True)
        return seq.extract(self._run_raw([(seq.nbits, seq.tms, seq.tdi)])[0])

    def dr_shift(self, value: int, nbits: int) -> int:
        seq = tapseq.dr_scan(value, nbits, from_reset=False)
        return seq.extract(self._run_raw([(seq.nbits, seq.tms, seq.tdi)])[0])

    def jtag_sequence(self, steps: list[tuple[int, int, int, int]]) -> list[int | None]:
        """Run every IR/DR pair inside one JLink.exe invocation.

        This is the only way this backend can honour the pairing, because TAP
        state is scoped to a single run: separate calls would each reconnect and
        reset, losing the loaded instruction.

        The first IR shift resets the TAP first; the rest do not, so each data
        register is shifted while its own instruction is still loaded.
        """
        vectors: list[tuple[int, int, int]] = []
        dr_seqs = []
        for i, (ir_value, ir_bits, dr_value, dr_bits) in enumerate(steps):
            ir = tapseq.ir_scan(ir_value, ir_bits, from_reset=(i == 0))
            dr = tapseq.dr_scan(dr_value, dr_bits, from_reset=False)
            vectors.append((ir.nbits, ir.tms, ir.tdi))
            vectors.append((dr.nbits, dr.tms, dr.tdi))
            dr_seqs.append(dr)

        captures = self._run_raw(vectors)
        # Captures alternate IR, DR, IR, DR...; only the DR ones carry payload.
        return [seq.extract(captures[2 * i + 1]) for i, seq in enumerate(dr_seqs)]

    def cross_check_dr(self, value: int, nbits: int) -> int:
        """Shift a DR through J-Link's own WJTAGDR as an oracle for raw_shift().

        Limited to 64 bits by the command's Data64 argument. Useful for proving
        the raw path against a second, independent route to the same answer.
        """
        if nbits > MAX_WJTAGDR_BITS:
            raise TransportError(
                f"WJTAGDR carries at most {MAX_WJTAGDR_BITS} bits and {nbits} were requested"
            )
        text = self.run_script(self._bringup_commands() + [_wjtagdr(value, nbits)])
        got = parsing.parse_returns(text)
        if got is None:
            err = parsing.find_error(text)
            raise TransportError(f"WJTAGDR returned no value{f' ({err})' if err else ''}")
        return got

    # --------------------------------------------------- unsupported here --

    def _no_single_shot(self, what: str) -> TransportError:
        return TransportError(
            f"the commander transport cannot do a single {what}: it runs JLink.exe in "
            "batch mode, so DP SELECT state does not survive between calls. Use "
            "--transport dll for interactive DAP work, or read_dap_batch()/run_script() "
            "for a one-shot cross-check."
        )

    def dp_reg_read(self, index: int) -> int:
        raise self._no_single_shot("DP read")

    def dp_reg_write(self, index: int, value: int) -> None:
        raise self._no_single_shot("DP write")

    def ap_reg_read(self, index: int) -> int:
        raise self._no_single_shot("AP read")

    def ap_reg_write(self, index: int, value: int) -> None:
        raise self._no_single_shot("AP write")
