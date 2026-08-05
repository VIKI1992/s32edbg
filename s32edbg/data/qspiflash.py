"""External QSPI-NOR flash tables for the `flash` flow -- every number cited.

Provenance
----------
The user-designated reference for external-flash access is the s32multitool
project (``C:/s32multitool``). Its supported route programs the QuadSPI_0
controller directly over the debug port and runs a small Cortex-M33 RAM applet
for the flash-bus operations. The tables below transcribe that route
**verbatim** -- same registers, same values, same order -- from:

* ``src/bootcfg_tool/resources/flash_profiles/scripts/s32z2e2_flash_MX25UW25A45G.py``
  (NXP, BSD-3-Clause): ``config_pll`` (:23-40), ``config_m33`` (:43-47),
  ``config_qspi_pins`` (:50-74), ``qspi`` (:77-157) -- the PLL / M33-clock /
  pad / controller bring-up that must run before any flash access.
* ``src/bootcfg_tool/resources/flash_profiles/scripts/bootcfg_raw_qspi_mx25uw25a45g.py``:
  module constants (:5-46), the raw-ops touch-up (:371-387, :810-842), flag
  clearing (:844-852), both LUT sets (:398-510), the LUT encoders
  (:1227-1233), the JEDEC validity predicate (:1235-1237), and the applet
  call contract (:996-1132).
* ``src/bootcfg_tool/resources/flash_profiles/scripts/bootcfg_raw_qspi_m33_applet.c``:
  return codes (:79-88) and phase codes (:90-108). The applet *binary* is
  vendored unmodified next to this module (see ``applet_bytes``); it is
  toolchain-locked (S32DS 3.6.1 GCC 9.2) and must never be rebuilt casually
  (``docs/RAW_QSPI_APPLET.md:24-40``).
* ``flash_address_sets.json``: the named flash regions.

The one deliberate divergence from the NXP-derived init: ``ACTIONS_RAW_TOUCHUP``
forces **SFACR = 0**. The S32DS controller init leaves SFACR = 0x00010803
(column address + word addressing), which silently transforms the SFAR offset
the controller emits and aliased multi-page programs on the reference bench
(NXP AN13563 3.3.1.2; ``docs/RAW_QSPI_APPLET.md:185-195``). The reference raw
driver applies the same correction before every operation
(``bootcfg_raw_qspi_mx25uw25a45g.py:827-833``).

Device: Macronix MX25UW25A45G, 32 MiB (256 Mb) 1.8 V octal NOR on QuadSPI_0.
"""

from __future__ import annotations

import hashlib
from importlib import resources

from .rtuboot import InitAction, _poll, _rmw, _sleep, _w

# --------------------------------------------------------------------------
# QuadSPI_0 controller -- base and register offsets
# (bootcfg_raw_qspi_mx25uw25a45g.py:5, :864-905; applet .c:3-22)
# --------------------------------------------------------------------------

QSPI_BASE = 0x4232_0000

QSPI_MCR = 0x000
QSPI_IPCR = 0x008
QSPI_FLSHCR = 0x00C
QSPI_BUF0CR = 0x010
QSPI_BUF1CR = 0x014
QSPI_BUF2CR = 0x018
QSPI_BUF3CR = 0x01C
QSPI_BFGENCR = 0x020
QSPI_BUF0IND = 0x030
QSPI_BUF1IND = 0x034
QSPI_BUF2IND = 0x038
QSPI_AWRCR = 0x050
QSPI_DLLCRA = 0x060
QSPI_SFAR = 0x100
QSPI_SFACR = 0x104
QSPI_SMPR = 0x108
QSPI_RBSR = 0x10C
QSPI_DLLSR = 0x12C
QSPI_TBSR = 0x150
QSPI_TBDR = 0x154
QSPI_TBCT = 0x158
QSPI_SR = 0x15C
QSPI_FR = 0x160
QSPI_SPTRCLR = 0x16C
QSPI_SFA1AD = 0x180
QSPI_SFA2AD = 0x184
QSPI_RBDR = 0x200
QSPI_LUTKEY = 0x300
QSPI_LCKCR = 0x304
QSPI_LUT_BASE = 0x310
QSPI_LUT_STRIDE = 0x14
QSPI_FRAD0_WORD1 = 0x804
QSPI_FRAD0_WORD2 = 0x808
QSPI_FRAD0_WORD3 = 0x80C
QSPI_TGMDAD0 = 0x900
QSPI_TG0SFARS = 0x908
QSPI_TG0IPCRS = 0x90C
QSPI_TG1SFARS = 0x918
QSPI_TG1IPCRS = 0x91C
QSPI_MGC = 0x920
QSPI_MRC = 0x924
QSPI_MTO = 0x928
QSPI_FLSEQREQ = 0x92C
QSPI_FSMSTAT = 0x930
QSPI_IPSERROR = 0x934
QSPI_ERRSTAT = 0x938
#: Written 0 by the reference before raw ops (install_luts :374); the register
#: is not in its own dump list -- kept verbatim, unnamed there too.
QSPI_REG_93C = 0x93C

#: SR bit 0 -- IP access in progress (wait_ip_done :854-862).
QSPI_SR_IP_BUSY = 0x0000_0001
#: SR mask the reference's wait_controller_idle polls to zero (:1212-1221).
QSPI_SR_IDLE_MASK = 0x0000_0051
#: MCR bits 11:10 -- TX/RX buffer clear, self-clearing (clear_buffers :801-808).
QSPI_MCR_CLR_TXRX = 0x0000_0C00
#: SPTRCLR write value + the bit that must self-clear (:835-842).
QSPI_SPTRCLR_CLEAR = 0x0001_0101
QSPI_SPTRCLR_BUSY = 0x0001_0000
#: LUT lock machinery (:970-976).
QSPI_LUTKEY_VALUE = 0x5AF0_5AF0
QSPI_LCKCR_UNLOCK = 0x0000_0002
QSPI_LCKCR_LOCK = 0x0000_0001

# --------------------------------------------------------------------------
# Flash device geometry and identity (:6-10, memory_tools.py:75-93)
# --------------------------------------------------------------------------

#: SFAR offsets are raw flash offsets -- flash_base stays 0 (:6, memory_tools
#: passes it through unchanged; a nonzero base re-introduces address aliasing).
IP_FLASH_BASE = 0x0000_0000
PAGE_SIZE = 0x100  # QSPI_TX_WINDOW -- program window and alignment unit
SECTOR_SIZE = 0x1000
BLOCK_ERASE_SIZE = 0x1_0000
DEVICE_SIZE = 0x0200_0000  # 32 MiB (memory_tools.py:82 window_size)

#: memory_tools.py:93 -- the only accepted JEDEC identity.
EXPECTED_JEDEC_IDS = (0xC239_89C2,)

#: Candidate AHB-window bases for memory-mapped reads. The reference's raw
#: path reads at mapped_base = 0x0 (memory_tools.py:80, :2496-2499); the
#: profile also carries ahb_read_base = 0x08000000 for the legacy service
#: path (memory_tools.py:81). Which one answers on which AP is a bench
#: question -- both are probed.
AHB_WINDOW_BASES = (0x0000_0000, 0x0800_0000)

FLASH_WIP = 0x01
FLASH_WEL = 0x02

# --------------------------------------------------------------------------
# Chunking and timeouts (:16, :623-661; chip erase :345-363)
# --------------------------------------------------------------------------

#: One applet program/read call moves at most this much payload (:15-16).
PROGRAM_CHUNK = 0x3_0000
#: One applet erase call covers at most this much flash (:634-645 default).
ERASE_CHUNK = 0x10_0000
#: Host-side AHB read chunk (:656-661 default).
AHB_READ_CHUNK = 0x1_0000

IP_TIMEOUT_S = 2.0  # wait_ip_done as used by RDID/RDSR/WREN/chip-erase issue
WREN_TIMEOUT_S = 2.0  # write_enable WEL latch deadline (:565)
BUFFER_CLEAR_TIMEOUT_S = 1.0  # clear_buffers (:803)
SPTRCLR_TIMEOUT_S = 1.0  # clear_ahb_sequence_pointer (:837)
CHIP_ERASE_TIMEOUT_S = 900.0  # wait_ready("chip_erase", 900.0) (:361)

# --------------------------------------------------------------------------
# The M33 RAM applet contract (:11-32; .ld via docs/RAW_QSPI_APPLET.md:90-112)
# --------------------------------------------------------------------------

APPLET_FILENAME = "bootcfg_raw_qspi_m33_applet.bin"
APPLET_SIZE = 2072
APPLET_SHA256 = "0c023a43be281485e6b41ca90401faec6c16c3cf9f7618c551d5a4229899f12d"

APPLET_BASE = 0x2500_0000
#: Entry PC. The reference sets ``$pc = base | 1`` through GDB, which owns the
#: Thumb-bit convention; through DCRSR the DebugReturnAddress is written
#: without bit 0 and EPSR.T is forced via the xpsr write instead.
APPLET_ENTRY = APPLET_BASE
APPLET_STATUS = 0x2500_2000
APPLET_STATUS_MAGIC = 0x4251_4347  # "BQCG" little-endian (.c:31)
APPLET_DATA = 0x2500_3000
APPLET_DATA_SIZE = 0x3_0000
APPLET_STACK = 0x2503_4000

OP_PROGRAM_BUFFER = 3
OP_READ_BUFFER = 4
OP_ERASE_4K_RANGE = 5
OP_ERASE_64K_RANGE = 6

#: Applet return codes (.c:79-88). 0 is success.
RC_NAMES: dict[int, str] = {
    0x1001: "BAD_OP",
    0x1002: "BAD_ARGUMENT",
    0x1003: "FSM_STATE1_TIMEOUT",
    0x1004: "FSM_STATE2_TIMEOUT",
    0x1005: "IP_TIMEOUT",
    0x1006: "QSPI_ERROR",
    0x1007: "WREN_TIMEOUT",
    0x1008: "READY_TIMEOUT",
    0x1009: "BUFFER_RANGE",
}

#: The 28-word status mailbox at APPLET_STATUS, in order (:1091-1120).
MAILBOX_LABELS: tuple[str, ...] = (
    "MAGIC",
    "OP",
    "RC",
    "PHASE",
    "SR",
    "FR",
    "FSMSTAT",
    "ERRSTAT",
    "IPSERROR",
    "TBSR",
    "TBCT",
    "TOTAL_SIZE",
    "WORDS_WRITTEN",
    "FIRST_WORD",
    "LAST_WORD",
    "SPTRCLR",
    "REQUESTED_OFFSET",
    "REQUESTED_SIZE",
    "SFAR_BEFORE",
    "SFAR_AFTER_WRITE",
    "IPCR_WRITTEN",
    "IPCR_AFTER_WRITE",
    "FINAL_SFAR",
    "FINAL_IPCR",
    "TG0SFARS",
    "TG0IPCRS",
    "TG1SFARS",
    "TG1IPCRS",
)


def applet_bytes() -> bytes:
    """The vendored applet binary, identity-checked on every load.

    The SHA-pin is what makes "the constants in this module describe that
    binary" a fact rather than a hope; a mismatch means the vendored file was
    touched and every address above is suspect.
    """
    blob = (resources.files(__package__) / APPLET_FILENAME).read_bytes()
    if len(blob) != APPLET_SIZE:
        raise ValueError(
            f"vendored applet is {len(blob)} bytes, expected {APPLET_SIZE}. "
            "The binary was modified; refusing to run it."
        )
    digest = hashlib.sha256(blob).hexdigest()
    if digest != APPLET_SHA256:
        raise ValueError(
            f"vendored applet SHA-256 {digest} does not match the pinned "
            f"{APPLET_SHA256}. The binary was modified; refusing to run it."
        )
    return blob


# --------------------------------------------------------------------------
# LUT machinery (:20-27, :398-510, :1227-1233)
# --------------------------------------------------------------------------

#: RTD copies one virtual LUT command into physical slot 0 before each IP
#: transaction; the reference keeps that model ("treating virtual LUT IDs as
#: physical sequence IDs was enough to corrupt multi-page program
#: addressing", :20-23). The applet assumes these fixed slots (.c:41-44) --
#: including issuing *erase* through slot 0, which its source calls
#: SEQ_PAGE_PROGRAM (.c:595).
PHYS_COMMAND_LUT = 0
PHYS_AHB_LUT = 1
PHYS_WREN_LUT = 2
PHYS_RDSR_LUT = 3
LUT_SEQUENCE_REGS = 5

#: READ sequence data operand (:7).
READ_LUT_OPERAND = 16

LUT_WREN = "wren"
LUT_RDID = "rdid"
LUT_RDSR = "rdsr"
LUT_WRDI = "wrdi"
LUT_READ = "read"
LUT_PAGE_PROGRAM = "page_program"
LUT_SECTOR_ERASE = "sector_erase"
LUT_BLOCK_ERASE_64K = "block_erase_64k"
LUT_CHIP_ERASE = "chip_erase"


def lut_instr(instr_code: int, pads: int, operand: int) -> int:
    """One 16-bit LUT halfword (:1227-1229). Instr codes used: 1=CMD, 2=ADDR,
    3=DUMMY, 7=READ, 8=WRITE, 0=STOP. Pads: 0 = 1-pad (SPI), 3 = 8-pad (OPI)."""
    return ((instr_code & 0x3F) << 10) | ((pads & 0x3) << 8) | (operand & 0xFF)


def lut_pair(first: int, second: int = 0) -> int:
    """Two halfwords packed into one 32-bit LUT register (:1231-1233)."""
    return (first & 0xFFFF) | ((second & 0xFFFF) << 16)


#: SPI (1-pad) sequences, verbatim from spi_lut_sequences (:398-447).
SPI_LUTS: dict[str, tuple[int, ...]] = {
    LUT_RDID: (lut_instr(1, 0, 0x9F), lut_instr(7, 0, 4), 0),
    LUT_WREN: (lut_instr(1, 0, 0x06), 0),
    LUT_RDSR: (lut_instr(1, 0, 0x05), lut_instr(7, 0, 1), 0),
    LUT_WRDI: (lut_instr(1, 0, 0x04), 0),
    LUT_READ: (
        lut_instr(1, 0, 0x0B),
        lut_instr(2, 0, 24),
        lut_instr(3, 0, 8),
        lut_instr(7, 0, READ_LUT_OPERAND),
        0,
    ),
    LUT_PAGE_PROGRAM: (
        lut_instr(1, 0, 0x02),
        lut_instr(2, 0, 24),
        lut_instr(8, 0, 8),
        0,
    ),
    LUT_SECTOR_ERASE: (lut_instr(1, 0, 0x20), lut_instr(2, 0, 24), 0),
    LUT_BLOCK_ERASE_64K: (lut_instr(1, 0, 0xDC), lut_instr(2, 0, 32), 0),
    LUT_CHIP_ERASE: (lut_instr(1, 0, 0x60), 0),
}

#: OPI (8-pad) sequences, verbatim from opi_lut_sequences (:449-510). Every
#: Macronix OPI command is the opcode plus its complement.
OPI_LUTS: dict[str, tuple[int, ...]] = {
    LUT_RDID: (
        lut_instr(1, 3, 0x9F),
        lut_instr(1, 3, 0x60),
        lut_instr(2, 3, 32),
        lut_instr(3, 3, 4),
        lut_instr(7, 3, 4),
        0,
    ),
    LUT_WREN: (lut_instr(1, 3, 0x06), lut_instr(1, 3, 0xF9), 0),
    LUT_RDSR: (
        lut_instr(1, 3, 0x05),
        lut_instr(1, 3, 0xFA),
        lut_instr(2, 3, 32),
        lut_instr(3, 3, 4),
        lut_instr(7, 3, 1),
        0,
    ),
    LUT_WRDI: (lut_instr(1, 3, 0x04), lut_instr(1, 3, 0xFB), 0),
    LUT_READ: (
        lut_instr(1, 3, 0xEC),
        lut_instr(1, 3, 0x13),
        lut_instr(2, 3, 32),
        lut_instr(3, 3, 20),
        lut_instr(7, 3, READ_LUT_OPERAND),
        0,
    ),
    LUT_PAGE_PROGRAM: (
        lut_instr(1, 3, 0x12),
        lut_instr(1, 3, 0xED),
        lut_instr(2, 3, 32),
        lut_instr(8, 3, 8),
        0,
    ),
    LUT_SECTOR_ERASE: (
        lut_instr(1, 3, 0x21),
        lut_instr(1, 3, 0xDE),
        lut_instr(2, 3, 32),
        0,
    ),
    LUT_BLOCK_ERASE_64K: (
        lut_instr(1, 3, 0xDC),
        lut_instr(1, 3, 0x23),
        lut_instr(2, 3, 32),
        0,
    ),
    LUT_CHIP_ERASE: (lut_instr(1, 3, 0x60), lut_instr(1, 3, 0x9F), 0),
}


def luts_for_mode(mode: str) -> dict[str, tuple[int, ...]]:
    """lut_sequences_for_mode (:393-396): anything but "opi" means SPI."""
    return OPI_LUTS if mode == "opi" else SPI_LUTS


def is_invalid_jedec(value: int) -> bool:
    """JEDEC reads that mean "nothing answered" (:1235-1237)."""
    return value in (0x0000_0000, 0xFFFF_FFFF) or (value & 0xFFFF_FF00) == 0xFFFF_FF00


# --------------------------------------------------------------------------
# Bring-up tables -- s32z2e2_flash_MX25UW25A45G.py, transcribed in order.
# Poll timeouts: the source's read_mem_compare32_le retries "100" times; this
# tool's run_init_actions floors poll timeouts at 1 s (rtuboot.py note), which
# only slows failure, never success.
# --------------------------------------------------------------------------

#: config_pll (:23-40). PLL @0x40220000, dividers, then MC_CGM4 MUX7 onto it.
ACTIONS_PLL: tuple[InitAction, ...] = (
    _w(0x4022_0020, 0x0000_0001, "PLLCLKMUX"),
    _w(0x4022_0008, 0x0C3F_1028, "PLLDV"),
    _w(0x4022_0000, 0x0000_0000, "PLLCR (enable)"),
    _sleep(200),
    _poll(0x4022_0004, 0x0000_0004, 0x0000_0004, 100, "PLLSR lock"),
    _w(0x4022_0098, 0x8034_0000, "PLLODIV[6]"),
    _w(0x4203_04C0, 0x1A00_0004, "MC_CGM4_MUX7_CSC"),
    _sleep(200),
    _poll(0x4203_04C4, 0x0001_0000, 0x0000_0000, 100, "MC_CGM4_MUX7_CSS switch done"),
    _w(0x4203_04C8, 0x8000_0000, "MC_CGM4_MUX7_DC_0"),
    _sleep(200),
    _poll(0x4203_04FC, 0x0000_0001, 0x0000_0000, 100, "MC_CGM4_MUX7 divider update done"),
)

#: config_m33 (:43-47). The M33 subsystem clock mux.
ACTIONS_M33_CLOCK: tuple[InitAction, ...] = (
    _w(0x4103_0300, 0x0F00_0004, "MC_CGM2_MUX0_CSC enable"),
    _sleep(200),
    _poll(0x4103_0304, 0x0001_0000, 0x0000_0000, 100, "MC_CGM2_MUX0_CSS switch done"),
)

#: config_qspi_pins (:50-74) -- 19 SIUL2_4 MSCRs for QuadSPI A. Labels carry
#: the source's pad numbers.
ACTIONS_QSPI_PADS: tuple[InitAction, ...] = (
    _w(0x4252_03D0, 0x0028_30F1, "SIUL2_4 MSCR[100]"),
    _w(0x4252_03B8, 0x0028_30F1, "SIUL2_4 MSCR[94]"),
    _w(0x4252_03E0, 0x0028_30F1, "SIUL2_4 MSCR[104]"),
    _w(0x4252_03C4, 0x0028_30F1, "SIUL2_4 MSCR[97]"),
    _w(0x4252_03D4, 0x0028_30F1, "SIUL2_4 MSCR[101]"),
    _w(0x4252_03BC, 0x0028_30F1, "SIUL2_4 MSCR[95]"),
    _w(0x4252_03DC, 0x0028_30F1, "SIUL2_4 MSCR[103]"),
    _w(0x4252_03C0, 0x0028_30F1, "SIUL2_4 MSCR[96]"),
    _w(0x4252_03D8, 0x0028_20F1, "SIUL2_4 MSCR[102]"),
    _w(0x4252_03CC, 0x0020_20F1, "SIUL2_4 MSCR[99]"),
    _w(0x4252_03C8, 0x0020_20F1, "SIUL2_4 MSCR[98]"),
    _w(0x4252_03B4, 0x0020_30F1, "SIUL2_4 MSCR[93]"),
    _w(0x4252_03E4, 0x0020_30F1, "SIUL2_4 MSCR[105]"),
    _w(0x4252_0484, 0x0028_20F1, "SIUL2_4 MSCR[145]"),
    _w(0x4252_0480, 0x0028_20F1, "SIUL2_4 MSCR[144]"),
    _w(0x4252_0474, 0x0028_20F1, "SIUL2_4 MSCR[141]"),
    _w(0x4252_0470, 0x0028_20F1, "SIUL2_4 MSCR[140]"),
    _w(0x4252_047C, 0x0020_30F1, "SIUL2_4 MSCR[143]"),
    _w(0x4252_0478, 0x0020_30F1, "SIUL2_4 MSCR[142]"),
)

#: qspi() (:77-157). Internal DQS pad loopback, SPI x1 mode, AHB and IP modes
#: configured. Note SFACR is set to the S32DS value 0x00010803 here and
#: forced to 0 by ACTIONS_RAW_TOUCHUP before raw ops -- same order as the
#: reference (init then touch-up), preserved so a bench comparison against
#: the reference sees identical register history.
ACTIONS_QSPI_CONTROLLER: tuple[InitAction, ...] = (
    _w(QSPI_BASE + QSPI_MCR, 0x000F_004F, "MCR disable"),
    _w(QSPI_BASE + QSPI_MCR, 0x000F_404C, "MCR disable, DQS loopback"),
    _sleep(200),
    _w(QSPI_BASE + QSPI_SFA1AD, 0x0400_0000, "SFA1AD top address"),
    _w(QSPI_BASE + QSPI_SFA2AD, 0x0400_0000, "SFA2AD top address"),
    _w(QSPI_BASE + QSPI_TGMDAD0, 0x8000_DFFF, "MDAD[0].TGMDAD"),
    _w(QSPI_BASE + QSPI_FRAD0_WORD1, 0x0800_0000, "FRAD[0].WORD1"),
    _w(QSPI_BASE + QSPI_FRAD0_WORD2, 0x0000_003F, "FRAD[0].WORD2"),
    _w(QSPI_BASE + QSPI_FRAD0_WORD3, 0x8000_0000, "FRAD[0].WORD3"),
    _w(QSPI_BASE + QSPI_BUF0CR, 0x0000_203F, "BUF0CR"),
    _w(QSPI_BASE + QSPI_BUF0IND, 0x0000_0000, "BUF0IND"),
    _w(QSPI_BASE + QSPI_AWRCR, 0x0000_0001, "AWRCR"),
    _w(QSPI_BASE + QSPI_BUF1CR, 0x0000_203F, "BUF1CR"),
    _w(QSPI_BASE + QSPI_BUF1IND, 0x0000_0000, "BUF1IND"),
    _w(QSPI_BASE + QSPI_BUF2CR, 0x0000_203F, "BUF2CR"),
    _w(QSPI_BASE + QSPI_BUF2IND, 0x0000_0000, "BUF2IND"),
    _w(QSPI_BASE + QSPI_BUF3CR, 0x8000_2015, "BUF3CR"),
    _w(QSPI_BASE + QSPI_FR, 0xFFFF_FFFF, "FR clear"),
    _w(QSPI_BASE + QSPI_MCR, 0x070F_40CC, "MCR"),
    _w(QSPI_BASE + QSPI_FLSHCR, 0x0001_0303, "FLSHCR"),
    _w(QSPI_BASE + QSPI_SFACR, 0x0001_0803, "SFACR (S32DS value; touch-up forces 0)"),
    _w(QSPI_BASE + QSPI_MCR, 0x070F_00CC, "MCR enable"),
    _w(QSPI_BASE + QSPI_DLLCRA, 0x0000_0006, "DLLCRA"),
    _w(QSPI_BASE + QSPI_DLLCRA, 0x0000_0007, "DLLCRA slave update"),
    _sleep(200),
    _poll(QSPI_BASE + QSPI_DLLSR, 0x0000_4000, 0x0000_4000, 100, "DLLSR slave lock"),
    _w(QSPI_BASE + QSPI_DLLCRA, 0x0000_0006, "DLLCRA"),
    _rmw(QSPI_BASE + QSPI_MCR, QSPI_MCR_CLR_TXRX, QSPI_MCR_CLR_TXRX, "MCR clear TX/RX"),
    _w(QSPI_BASE + QSPI_FR, 0xFFFF_FFFF, "FR clear"),
    _w(QSPI_BASE + QSPI_MCR, 0x070F_40CC, "MCR disable"),
    _w(QSPI_BASE + QSPI_SMPR, 0x0700_0000, "SMPR"),
    _w(QSPI_BASE + QSPI_MCR, 0x000F_00CC, "MCR enable"),
    _rmw(QSPI_BASE + QSPI_MCR, QSPI_MCR_CLR_TXRX, QSPI_MCR_CLR_TXRX, "MCR clear TX/RX"),
)

#: install_luts prologue + configure_controller_for_raw_ops +
#: configure_s32ds_ahb_window + clear_ahb_sequence_pointer, in the reference's
#: order (:371-375, :810-842). Runs before the LUT slots are (re)loaded.
ACTIONS_RAW_TOUCHUP: tuple[InitAction, ...] = (
    _w(QSPI_BASE + QSPI_MTO, 0xFFFF_FFFF, "MTO: no master timeout"),
    _w(QSPI_BASE + QSPI_REG_93C, 0x0000_0000, "0x93C cleared"),
    _w(QSPI_BASE + QSPI_SFACR, 0x0000_0000, "SFACR = 0 (AN13563 address aliasing fix)"),
    _w(QSPI_BASE + QSPI_BUF0IND, 0x0000_0000, "BUF0IND"),
    _w(QSPI_BASE + QSPI_BUF1IND, 0x0000_0000, "BUF1IND"),
    _w(QSPI_BASE + QSPI_BUF2IND, 0x0000_0000, "BUF2IND"),
    _w(QSPI_BASE + QSPI_BUF0CR, 0x0000_203F, "BUF0CR"),
    _w(QSPI_BASE + QSPI_BUF1CR, 0x0000_203F, "BUF1CR"),
    _w(QSPI_BASE + QSPI_BUF2CR, 0x0000_203F, "BUF2CR"),
    _w(QSPI_BASE + QSPI_BUF3CR, 0x8000_2015, "BUF3CR"),
    _w(QSPI_BASE + QSPI_AWRCR, 0x0000_0001, "AWRCR"),
    _w(QSPI_BASE + QSPI_BFGENCR, PHYS_AHB_LUT << 12, "BFGENCR: AHB reads use LUT 1"),
    _w(QSPI_BASE + QSPI_SPTRCLR, QSPI_SPTRCLR_CLEAR, "SPTRCLR: reset sequence pointers"),
    _poll(QSPI_BASE + QSPI_SPTRCLR, QSPI_SPTRCLR_BUSY, 0x0000_0000, 1000, "SPTRCLR self-clears"),
)

# --------------------------------------------------------------------------
# Flag clearing -- clear_flags (:844-852). The SFP clear split is deliberate
# and verified against the IC7 tool (docs/PROBE_BACKEND.md:170): *SFARS and
# FLSEQREQ/IPSERROR clear with 0x20000000, *IPCRS with 0x10000000.
# --------------------------------------------------------------------------

CLEAR_FLAGS_WRITES: tuple[tuple[int, int, str], ...] = (
    (QSPI_FR, 0xFFFF_FFFF, "FR"),
    (QSPI_ERRSTAT, 0x0000_01FF, "ERRSTAT"),
    (QSPI_FLSEQREQ, 0x2000_0000, "FLSEQREQ[CLR]"),
    (QSPI_IPSERROR, 0x2000_0000, "IPSERROR[CLR]"),
    (QSPI_TG0SFARS, 0x2000_0000, "TG0SFARS[CLR]"),
    (QSPI_TG0IPCRS, 0x1000_0000, "TG0IPCRS[CLR]"),
    (QSPI_TG1SFARS, 0x2000_0000, "TG1SFARS[CLR]"),
    (QSPI_TG1IPCRS, 0x1000_0000, "TG1IPCRS[CLR]"),
)

# --------------------------------------------------------------------------
# Named flash regions -- flash_address_sets.json, keys shortened for the CLI,
# labels kept verbatim.
# --------------------------------------------------------------------------


REGIONS: dict[str, tuple[int, int, str]] = {
    "blob": (0x0000_0000, 0x0020_0000, "Blob Area"),
    "bl": (0x0000_2000, 0x000F_D000, "BL Area"),
    "shared": (0x0010_0000, 0x0000_2000, "Shared Flash"),
    "selftest": (0x001D_C000, 0x0000_4000, "SelfTest Area"),
    "sysimg": (0x001E_0000, 0x0002_0000, "SysImg Area"),
    "selftestbck": (0x001F_C000, 0x0000_4000, "SelfTestBck Area"),
    "app": (0x0030_0000, 0x0060_0000, "Application Area"),
    "cert": (0x00A0_0000, 0x0000_5000, "Cert Area"),
}
