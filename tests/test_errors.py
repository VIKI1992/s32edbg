"""Regression tests for the exception hierarchy.

These exist because of a bug that the rest of the suite could not see. Every
recovery path in the tool catches DapError, but DllTransport raised
TransportError for a failed DP/AP access -- and the two are siblings, so none of
those handlers ever fired on real hardware. A single unreadable component
aborted an entire ROM walk. The suite stayed green because the test doubles
raise DapError, which is exactly the error the fakes were built to model.

No hardware required.
"""

from __future__ import annotations

import inspect

import pytest
from conftest import FakeTransport

from s32edbg.dap import apmap as apmap_mod
from s32edbg.dap import gates, romtable
from s32edbg.dap.adiv6 import AP_IDR, Dap
from s32edbg.data.expected_apmap import AP_APB
from s32edbg.errors import DapError, S32EError, TransportError
from s32edbg.transport import dll


def test_the_dll_transport_raises_dap_error_for_a_failed_access() -> None:
    """A failed CORESIGHT_ReadAPDPReg is a DAP fault, not a backend failure.

    Checked by reading the source rather than by calling it, because calling it
    needs a probe. The distinction matters: callers walking unknown address
    space catch DapError so one bad component is recorded and skipped, whereas a
    TransportError means the backend cannot do the operation at all and must
    propagate.
    """
    for name in ("dp_reg_read", "dp_reg_write", "ap_reg_read", "ap_reg_write"):
        src = inspect.getsource(getattr(dll.DllTransport, name))
        assert "raise DapError" in src, f"{name} does not raise DapError"
        assert "raise TransportError" not in src, f"{name} still raises TransportError"


def test_the_commander_transport_still_raises_transport_error_for_a_refusal() -> None:
    """"This backend cannot do single transactions" is a capability refusal.

    It must keep propagating rather than being swallowed as a per-component
    fault, otherwise a romdump over the commander backend would silently
    produce an empty tree instead of an error.
    """
    from s32edbg.transport import commander

    src = inspect.getsource(commander.CommanderTransport._no_single_shot)
    assert "TransportError" in src


def test_both_fault_kinds_share_the_tool_error_base() -> None:
    assert issubclass(DapError, S32EError)
    assert issubclass(TransportError, S32EError)


def test_an_unreadable_component_is_recorded_and_the_walk_continues() -> None:
    """ADIv6 p195 says a debugger meeting a faulting entry should "issue a
    warning ... [and] continue operating"."""
    t = FakeTransport(pipelined=False)
    t.unreadable.add(0x0000_0000 + romtable.OFF_CIDR0)

    root = romtable.walk(Dap(t).mem_reader(AP_APB), 0x0000_0000)
    assert root.error is not None
    assert romtable.summarise(root)["errors"] == 1


def test_an_unreadable_ap_is_reported_rather_than_propagating() -> None:
    t = FakeTransport(pipelined=False)
    t.unreadable.add(AP_APB + AP_IDR)

    result = apmap_mod.discover(Dap(t), methods=("direct",))
    entry = result.by_base()[AP_APB]
    assert entry.error is not None
    assert entry.idr is None


def test_a_gate_read_failure_is_captured_on_the_report() -> None:
    from s32edbg.data.expected_apmap import AP_MDM
    from s32edbg.data.ladder1 import MDM_IDR

    t = FakeTransport(pipelined=False)
    t.unreadable.add(AP_MDM + MDM_IDR)

    state = gates.read_mdm(Dap(t))
    assert state.error is not None


def test_a_dap_error_from_an_ap_read_does_not_escape_discovery() -> None:
    t = FakeTransport(pipelined=False)
    for ap in list(t.ap_space):
        t.unreadable.add(ap)
    t.unreadable.add(AP_APB + AP_IDR)

    # Must not raise.
    apmap_mod.discover(Dap(t), methods=("direct",))


def test_power_up_error_is_a_dap_error() -> None:
    from s32edbg.errors import PowerUpError

    assert issubclass(PowerUpError, DapError)


def test_a_transport_error_is_not_mistaken_for_a_dap_error() -> None:
    """They must stay distinguishable; the fix was to raise the right one, not
    to merge them."""
    assert not issubclass(TransportError, DapError)
    assert not issubclass(DapError, TransportError)


def test_a_faulting_root_yields_an_error_node_rather_than_an_exception() -> None:
    """The whole point of the fix: a fault becomes data, not a traceback."""
    t = FakeTransport(pipelined=False)
    t.unreadable.add(0x1000 + romtable.OFF_CIDR0)

    root = romtable.walk(Dap(t).mem_reader(AP_APB), 0x1000)
    assert root.error is not None
    assert root.children == []
