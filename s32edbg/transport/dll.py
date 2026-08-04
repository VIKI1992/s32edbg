"""ctypes transport against JLink_x64.dll.

There is no J-Link SDK on this machine -- no JLinkARMDLL.h, no import library,
and no UM08002 -- and no C compiler either. So the signatures below are
hand-written from the DLL's 636-symbol export table, and are therefore the least
certain part of this tool.

Two mitigations, both automatic:
  * every symbol is looked up defensively, so a missing export reports which
    function is absent instead of raising AttributeError deep in a call;
  * dap.adiv6.Dap.calibrate_ap_read() checks a known AP IDR against RM Table 957
    before anything else runs, which catches a wrong AP-read pipeline model, and
    the ``apmap`` command diffs the whole map against the RM.

Anything that cannot be validated that way is left to the Commander transport
rather than guessed at. In particular JLINK_JTAG_StoreRaw / StoreGetRaw are
exported but their prototypes appear nowhere on this machine, so they are not
bound here -- raw shifting goes through the Commander path.

Two traps worth recording, because the DLL's own string table invites both:

* JLINK_JTAG_ReadWriteBits, JLINK_JTAG_Reset and JLINK_SWD_ReadWriteBits appear
  in the binary with full, clean C prototypes and look like exactly the raw
  primitives this tool wants. They are NOT exported -- parsing the PE export
  directory and a ctypes getattr both say absent. They belong to the
  .JLinkScript built-in API, which only runs at fixed hooks (InitTarget,
  SetupTarget, ...) with no return channel. Do not plan around them.
* The StoreRaw family really is exported, but carries only printf format
  strings, and those disagree about argument order: StoreGetRaw logs its bit
  count first ("JLINK_JTAG_StoreGetRaw(0x%.2X Bits, ...)") while StoreRaw logs
  it last. So the order is not even inferable, let alone provable. This module
  has already paid once for a wrong ctypes signature (see SetLogFile below);
  raw shifting is not worth a second.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, c_char, c_char_p, c_int, c_uint8, c_uint32, c_void_p
from pathlib import Path

from ..errors import DapError, TransportError, UnsupportedOperation
from .base import ProbeInfo, Tif, Transport
from .install import JLinkInstall, discover

#: APnDP argument of CORESIGHT_ReadAPDPReg / WriteAPDPReg.
AP_NOT_DP_DP = 0
AP_NOT_DP_AP = 1


class DllTransport(Transport):
    name = "dll"
    #: Unknown whether the DLL wrapper de-pipelines JTAG-DP AP reads; nothing on
    #: this machine documents it. Start assuming it does (the opposite of the raw
    #: Commander path) and let calibrate_ap_read() correct it empirically.
    ap_read_pipelined = False
    #: No exported symbol here can shift an arbitrary TMS/TDI vector with a
    #: discoverable ABI, so raw JTAG belongs to the commander backend. See the
    #: module docstring.
    can_raw_shift = False
    raw_shift_returns_tdo = False

    def __init__(
        self,
        install: JLinkInstall | None = None,
        *,
        serial: str | None = None,
        log_file: str | Path | None = None,
        log_com: bool = True,
    ) -> None:
        self.install = install or discover()
        self.serial = serial
        self.log_file = Path(log_file) if log_file else None
        self.log_com = log_com
        self._lib: ctypes.CDLL | None = None
        self._opened = False
        self.capability_warnings: list[str] = []

    # ---------------------------------------------------------- lifecycle --

    @property
    def lib(self) -> ctypes.CDLL:
        if self._lib is None:
            raise TransportError("DLL transport is not open")
        return self._lib

    def _fn(self, *names: str):
        """Resolve the first available export from ``names``.

        Both a modern ``JLINK_*`` and a legacy ``JLINKARM_*`` family exist; most
        functions appear under both.
        """
        lib = self.lib
        for name in names:
            try:
                return getattr(lib, name)
            except AttributeError:
                continue
        raise TransportError(
            f"none of {', '.join(names)} is exported by {self.install.dll()}. "
            "This build of the J-Link DLL is not compatible with s32edbg."
        )

    def open(self) -> None:
        if self._opened:
            return
        want64 = sys.maxsize > 2**32
        dll_path = self.install.dll(want_64bit=want64)
        if not dll_path.is_file():
            raise TransportError(f"J-Link DLL not found at {dll_path}")
        if not want64:
            raise TransportError(
                "s32edbg requires 64-bit Python so it can load JLink_x64.dll; "
                "JLinkARM.dll is 32-bit."
            )
        try:
            self._lib = ctypes.CDLL(str(dll_path))
        except OSError as exc:
            raise TransportError(f"could not load {dll_path}: {exc}") from exc

        self._bind()

        if self.log_file:
            # SetLogFile is the whole logging story here. JLINK_EnableLog and
            # JLINK_EnableLogCom take a *callback function pointer*, not a flag --
            # calling them with an int makes the DLL write through address 0x1 and
            # corrupts its state, which then faults inside Open(). Learned the hard
            # way; do not "re-enable" them without a real ctypes.CFUNCTYPE.
            self._set_log_file(str(self.log_file).encode())

        if self.serial:
            sn = int(self.serial, 10)
            if self._select_by_sn(c_uint32(sn)) != 0:
                raise TransportError(f"no J-Link with serial {self.serial}")

        err = self._open()
        if err:
            raise TransportError(f"JLINK_Open failed: {err.decode('utf-8', 'replace')}")
        self._opened = True

    def close(self) -> None:
        if self._opened and self._lib is not None:
            try:
                self._close()
            except Exception:
                pass
        self._opened = False
        self._lib = None

    def _bind(self) -> None:
        """Attach argtypes/restype to every function this transport uses."""
        self._open = self._fn("JLINK_Open", "JLINKARM_Open")
        self._open.restype = c_char_p
        self._open.argtypes = []

        self._close = self._fn("JLINK_Close", "JLINKARM_Close")
        self._close.restype = None
        self._close.argtypes = []

        self._tif_select = self._fn("JLINK_TIF_Select", "JLINKARM_TIF_Select")
        self._tif_select.restype = c_int
        self._tif_select.argtypes = [c_int]

        self._set_speed = self._fn("JLINK_SetSpeed", "JLINKARM_SetSpeed")
        self._set_speed.restype = None
        self._set_speed.argtypes = [c_uint32]

        self._exec_command = self._fn("JLINK_ExecCommand", "JLINKARM_ExecCommand")
        self._exec_command.restype = c_int
        self._exec_command.argtypes = [c_char_p, POINTER(c_char), c_int]

        self._coresight_configure = self._fn(
            "JLINK_CORESIGHT_Configure", "JLINKARM_CORESIGHT_Configure"
        )
        self._coresight_configure.restype = c_int
        self._coresight_configure.argtypes = [c_char_p]

        self._read_apdp = self._fn(
            "JLINK_CORESIGHT_ReadAPDPReg", "JLINKARM_CORESIGHT_ReadAPDPReg"
        )
        self._read_apdp.restype = c_int
        self._read_apdp.argtypes = [c_uint8, c_uint8, POINTER(c_uint32)]

        self._write_apdp = self._fn(
            "JLINK_CORESIGHT_WriteAPDPReg", "JLINKARM_CORESIGHT_WriteAPDPReg"
        )
        self._write_apdp.restype = c_int
        self._write_apdp.argtypes = [c_uint8, c_uint8, c_uint32]

        self._set_log_file = self._fn("JLINK_SetLogFile", "JLINKARM_SetLogFile")
        self._set_log_file.restype = None
        self._set_log_file.argtypes = [c_char_p]

        self._get_hw_version = self._fn("JLINK_GetHardwareVersion", "JLINKARM_GetHardwareVersion")
        self._get_hw_version.restype = c_uint32
        self._get_hw_version.argtypes = []

        self._get_dll_version = self._fn("JLINK_GetDLLVersion", "JLINKARM_GetDLLVersion")
        self._get_dll_version.restype = c_int
        self._get_dll_version.argtypes = []

        self._get_fw_string = self._fn("JLINK_GetFirmwareString", "JLINKARM_GetFirmwareString")
        self._get_fw_string.restype = None
        self._get_fw_string.argtypes = [POINTER(c_char), c_int]

        self._get_hw_status = self._fn("JLINK_GetHWStatus", "JLINKARM_GetHWStatus")
        self._get_hw_status.restype = c_int
        self._get_hw_status.argtypes = [c_void_p]

        self._config_jtag = self._fn("JLINK_ConfigJTAG", "JLINKARM_ConfigJTAG")
        self._config_jtag.restype = None
        self._config_jtag.argtypes = [c_int, c_int]

        self._connect = self._fn("JLINK_Connect", "JLINKARM_Connect")
        self._connect.restype = c_int
        self._connect.argtypes = []

        try:
            self._select_by_sn = self._fn(
                "JLINK_EMU_SelectByUSBSN", "JLINKARM_EMU_SelectByUSBSN"
            )
            self._select_by_sn.restype = c_int
            self._select_by_sn.argtypes = [c_uint32]
        except TransportError:
            self._select_by_sn = None  # type: ignore[assignment]

        # JLINK_JTAG_GetDeviceId(int DeviceIndex) is exported *and* carries a real
        # prototype in the binary. Being pointer-free it has none of the ABI risk
        # that keeps the StoreRaw family unbound, and it gives this backend an
        # IDCODE without any raw shifting.
        try:
            self._jtag_get_device_id = self._fn(
                "JLINK_JTAG_GetDeviceId", "JLINKARM_JTAG_GetDeviceId"
            )
            self._jtag_get_device_id.restype = c_uint32
            self._jtag_get_device_id.argtypes = [c_int]
        except TransportError:
            self._jtag_get_device_id = None  # type: ignore[assignment]

        # WriteMem: the `load` fast path. `int WriteMem(U32 addr, U32 count,
        # const void* data)` is the conventional signature but nothing on this
        # machine can verify it, and the return convention (0 vs bytes-written)
        # varies by DLL vintage -- so the loader treats anything other than
        # unambiguous success as a failure, verifies the first block by full
        # independent ADIv6 read-back, and demotes itself to the pure word path
        # on any doubt. It also routes through the DLL's *own* notion of the
        # current memory AP, moving SELECT/CSW/TAR beneath the Dap object: the
        # caller must invalidate those caches after every burst.
        try:
            self._write_mem = self._fn("JLINK_WriteMem", "JLINKARM_WriteMem")
            self._write_mem.restype = c_int
            self._write_mem.argtypes = [c_uint32, c_uint32, c_void_p]
        except TransportError:
            self._write_mem = None  # type: ignore[assignment]

        # nRESET pin control for the `load --reset pin` variant. void(void),
        # so the ABI risk is as small as a hand binding gets. Board note: on
        # this board nRESET ties to SBC-PMIC_RSTb through R439 (defect D-2), so
        # a pin pulse reaches the power-management chips -- which is why the
        # SDA-AP functional reset is the default and this is the fallback.
        try:
            self._set_reset = self._fn("JLINK_SetRESET", "JLINKARM_SetRESET")
            self._set_reset.restype = None
            self._set_reset.argtypes = []
            self._clr_reset = self._fn("JLINK_ClrRESET", "JLINKARM_ClrRESET")
            self._clr_reset.restype = None
            self._clr_reset.argtypes = []
        except TransportError:
            self._set_reset = None  # type: ignore[assignment]
            self._clr_reset = None  # type: ignore[assignment]

    # ------------------------------------------------------- bring-up -----

    def set_jtag_config(self, ir_pre: int = -1, dr_pre: int = -1) -> None:
        """Configure IRPre/DRPre. **Negative means auto-detect.**

        This matters more than it looks. Forcing IRPre=0/DRPre=0 stops J-Link
        running its own chain detection; the working recipe on this target is
        ``JTAGConf -1 -1``. SEGGER's release notes confirm the convention:
        "'JTAGConf' command line option did not support negative values which
        are required to configure auto-detect".
        """
        self._config_jtag(c_int(ir_pre), c_int(dr_pre))

    def select_device(self, name: str) -> str:
        """Select a device by name via the command-string interface."""
        return self.exec_command(f"Device = {name}")

    def connect_target(self) -> int:
        """Run J-Link's own connect sequence.

        Required before DP/AP access works: on a bare session that has only had
        TIF_Select and SetSpeed, every ReadDP returns 0 because the chain has
        never been scanned and the DAP link was never initialised. Returns 0 on
        success; a non-zero result still often leaves the DAP usable (this target
        reports a connect failure when the core is hidden behind a closed debug
        gate, yet the AP map enumerates fine).
        """
        return int(self._connect())

    # ------------------------------------------------------------ helpers --

    def exec_command(self, cmd: str) -> str:
        """JLINK_ExecCommand -- the J-Link "command strings" namespace.

        This is the route to ADIv6 features the typed script API cannot express,
        notably ``CORESIGHT_AddAP = BaseAddr = 0x.. Index=.. Type=..``, which
        SEGGER's release notes describe as "required for CoreSight SoC-600
        support".
        """
        buf = ctypes.create_string_buffer(512)
        self._exec_command(cmd.encode(), buf, ctypes.sizeof(buf) - 1)
        text = buf.value.decode("utf-8", "replace")
        if text:
            from . import parsing

            for w in parsing.find_capability_warnings(text):
                if w not in self.capability_warnings:
                    self.capability_warnings.append(w)
        return text

    def configure_coresight(self, config: str = "") -> None:
        """JLINK_CORESIGHT_Configure.

        Accepted keys, from the DLL's own contiguous key table:
        IRPre, DRPre, IRPost, DRPost, IRLenDevice, PerformTIFInit,
        SetDormantModeHandling. An empty string performs the interface init
        sequence (SEGGER's own sample comments it as "Perform SWD init sequence").
        """
        rc = self._coresight_configure(config.encode())
        if rc < 0:
            raise TransportError(f"CORESIGHT_Configure({config!r}) failed with {rc}")

    # -------------------------------------------------------------- probe --

    def probe_info(self) -> ProbeInfo:
        hw = int(self._get_hw_version())
        dll = int(self._get_dll_version())
        fwbuf = ctypes.create_string_buffer(256)
        self._get_fw_string(fwbuf, ctypes.sizeof(fwbuf) - 1)

        info = ProbeInfo(
            serial=self.serial,
            hardware_version=_decode_hw_version(hw),
            firmware=fwbuf.value.decode("utf-8", "replace") or None,
            dll_version=_decode_dll_version(dll),
        )
        vtref, pins = self._hw_status()
        info.vtref_mv = vtref
        info.pins = pins
        info.notes.append(f"J-Link install: {self.install.path} (v{self.install.version_str})")
        info.notes.append(
            "VTREF and pin states come from JLINK_GetHWStatus, whose struct layout "
            "is not documented on this machine; treat them as best-effort and "
            "cross-check with the commander transport."
        )
        info.capabilities = list(self.capability_warnings)
        return info

    def _hw_status(self) -> tuple[int | None, dict[str, int]]:
        """Best-effort JLINK_GetHWStatus decode.

        The struct is conventionally {U16 VTarget_mV; U8 tck,tdi,tdo,tms,tres,trst;}
        but that layout is not documented here, so a generously oversized buffer
        is used and only the leading fields are interpreted.
        """
        buf = ctypes.create_string_buffer(64)
        try:
            rc = self._get_hw_status(ctypes.cast(buf, c_void_p))
        except Exception:
            return None, {}
        if rc != 0:
            return None, {}
        raw = buf.raw
        vtref = int.from_bytes(raw[0:2], "little")
        names = ("TCK", "TDI", "TDO", "TMS", "TRES", "TRST")
        pins = {n: raw[2 + i] for i, n in enumerate(names) if raw[2 + i] in (0, 1)}
        return (vtref if 0 < vtref < 6000 else None), pins

    def select_interface(self, tif: Tif) -> None:
        self._tif_select(c_int(int(tif)))
        self.selected_tif = Tif(int(tif))

    def set_speed_khz(self, khz: int) -> None:
        self._set_speed(c_uint32(khz))

    def set_dormant_mode_handling(self, enable: bool = True) -> None:
        """Opt into the dormant-state protocol switch.

        The S32E's SWJ-DP implements the dormant state (RM 135.14.1.1), and Arm
        deprecates the direct 0x79E7/0x3CE7 sequences on such parts. The DLL
        implements DStoSWD/DStoJTAG/JTAGtoDS/SWDtoDS plus the selection alert,
        gated behind this setting.
        """
        self.exec_command(f"SetDormantModeHandling = {1 if enable else 0}")

    # ---------------------------------------------------------- raw JTAG ---

    def read_idcode(self, index: int = 0) -> int:
        """IDCODE of chain device ``index``, without shifting anything by hand.

        J-Link scans the chain during connect and caches each device's ID, so
        this needs none of the raw-vector machinery the commander backend uses.
        """
        if self._jtag_get_device_id is None:
            raise UnsupportedOperation(
                "this J-Link DLL does not export JLINK_JTAG_GetDeviceId"
            )
        return int(self._jtag_get_device_id(c_int(index))) & 0xFFFF_FFFF

    # ------------------------------------------------------------ DP / AP --

    # A failed transaction here is a DapError, not a TransportError. The
    # distinction is load-bearing: callers that walk unknown address space
    # (romtable.walk, apmap.discover, gates.read_mdm) catch DapError so a single
    # unreadable component is recorded and skipped rather than aborting the run.
    # A TransportError means the backend itself cannot do the operation, which
    # must propagate. Raising the wrong one here silently disabled every one of
    # those recovery paths on real hardware.

    def dp_reg_read(self, index: int) -> int:
        out = c_uint32(0)
        rc = self._read_apdp(c_uint8(index), c_uint8(AP_NOT_DP_DP), ctypes.byref(out))
        if rc < 0:
            raise DapError(f"CORESIGHT_ReadAPDPReg(DP,{index}) failed with {rc}")
        return int(out.value)

    def dp_reg_write(self, index: int, value: int) -> None:
        rc = self._write_apdp(c_uint8(index), c_uint8(AP_NOT_DP_DP), c_uint32(value & 0xFFFFFFFF))
        if rc < 0:
            raise DapError(f"CORESIGHT_WriteAPDPReg(DP,{index}) failed with {rc}")

    def ap_reg_read(self, index: int) -> int:
        out = c_uint32(0)
        rc = self._read_apdp(c_uint8(index), c_uint8(AP_NOT_DP_AP), ctypes.byref(out))
        if rc < 0:
            raise DapError(f"CORESIGHT_ReadAPDPReg(AP,{index}) failed with {rc}")
        return int(out.value)

    def ap_reg_write(self, index: int, value: int) -> None:
        rc = self._write_apdp(c_uint8(index), c_uint8(AP_NOT_DP_AP), c_uint32(value & 0xFFFFFFFF))
        if rc < 0:
            raise DapError(f"CORESIGHT_WriteAPDPReg(AP,{index}) failed with {rc}")

    # ------------------------------------------------- bulk memory / reset --

    def configure_axi_memory_ap(self) -> str:
        """Register the AXI-AP with the DLL and select it for memory access.

        Guide §7.2: `JLINK_CORESIGHT_AddAP()` is deprecated for SoC-600; the
        documented replacement is the command string, and
        `CORESIGHT_SetIndexAHBAPToUse` "specifies which AP index to use for
        memory access (ADIv6/SoC-600 support)". SEGGER documents these for
        ConfigTargetSettings() -- issuing them mid-session is not documented
        either way, which is why the WriteMem fast path proves the routing
        with a full independent read-back of its first block rather than
        trusting this call. Returns whatever the DLL printed, for the note.
        """
        out1 = self.exec_command("CORESIGHT_AddAP = Index=0 Type=AXI-AP Addr=0x01C20000")
        out2 = self.exec_command("CORESIGHT_SetIndexAHBAPToUse = 0")
        return (out1 + " " + out2).strip()

    @property
    def can_block_write(self) -> bool:  # type: ignore[override]
        return self._lib is not None and self._write_mem is not None

    def write_mem_block(self, addr: int, data: bytes) -> None:
        """Push a byte block through the DLL's own memory machinery.

        See the binding comment in _bind(): the signature is conventional but
        unverified, so only `rc == 0` (older convention: success) or
        `rc == len(data)` (newer: bytes written) counts as success -- anything
        else, including a wrong-looking positive, is a TransportError the
        caller's fallback catches. The DLL moves SELECT/CSW/TAR beneath the
        Dap object here; callers must invalidate those caches afterwards.
        """
        if self._write_mem is None:
            raise UnsupportedOperation(
                "this J-Link DLL does not export JLINK_WriteMem"
            )
        buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        rc = int(self._write_mem(c_uint32(addr), c_uint32(len(data)), ctypes.cast(buf, c_void_p)))
        if rc not in (0, len(data)):
            raise TransportError(
                f"JLINK_WriteMem(0x{addr:08X}, {len(data)}) returned {rc} -- "
                "not an unambiguous success under either return convention"
            )

    @property
    def can_pin_reset(self) -> bool:  # type: ignore[override]
        return (
            self._lib is not None
            and self._set_reset is not None
            and self._clr_reset is not None
        )

    def set_reset_pin(self, asserted: bool) -> None:
        if self._set_reset is None or self._clr_reset is None:
            raise UnsupportedOperation(
                "this J-Link DLL does not export SetRESET/ClrRESET"
            )
        # SEGGER naming: ClrRESET pulls the (active-low) line LOW = asserted.
        if asserted:
            self._clr_reset()
        else:
            self._set_reset()


def _decode_hw_version(raw: int) -> str | None:
    """J-Link encodes hardware version as major*10000 + minor*100."""
    if raw == 0:
        return None
    return f"V{raw // 10000}.{(raw // 100) % 100:02d}"


def _decode_dll_version(raw: int) -> str | None:
    if raw == 0:
        return None
    major = raw // 10000
    minor = (raw // 100) % 100
    patch = raw % 100
    suffix = chr(ord("a") + patch - 1) if patch else ""
    return f"V{major}.{minor:02d}{suffix}"
