"""端口流量在 CLI 上的可见性。"""
import pytest
from idc_simulator.cli import CliSession, counter_rates, render_counters, util_percent
from idc_simulator.control import Controller
from conftest import LocalClient

FLOW_BPS = 8_000_000
PACKETS = 10_000
BYTES = 10_000_000


def _session(controller, profile=None):
    if profile:
        controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["profile"] = profile
    client = LocalClient(controller)
    return CliSession(client), client


def _advance(controller, clock, seconds=10):
    clock.advance(seconds)
    controller.snapshot("sw-a")


def _brand_session(tmp_path, raw_lab, clock, profile):
    raw_lab["devices"][0]["profile"] = profile
    controller = Controller(tmp_path/"brand-state.db", clock=clock)
    controller.create(raw_lab)
    return controller, CliSession(LocalClient(controller))


@pytest.mark.asyncio
async def test_ios_show_interfaces_counters_and_detail(controller, clock):
    session, _ = _session(controller)
    _advance(controller, clock)
    table = await session.execute("show interfaces counters")
    assert "InOctets" in table and "Gi1/0/1" in table
    assert str(BYTES) in table
    detail = await session.execute("show interfaces Gi1/0/1")
    assert f"5 minute input rate {FLOW_BPS} bits/sec, 1000 packets/sec" in detail
    assert f"{PACKETS} packets input, {BYTES} bytes" in detail
    assert "5 minute output rate 0 bits/sec, 0 packets/sec" in detail


@pytest.mark.asyncio
async def test_counters_accept_a_port_argument(controller, clock):
    session, _ = _session(controller)
    _advance(controller, clock)
    single = await session.execute("show interfaces Gi1/0/1 counters")
    assert "Gi1/0/1" in single and "Gi1/0/2" not in single


@pytest.mark.asyncio
async def test_unknown_interface_is_reported_not_silently_empty(controller, clock):
    session, _ = _session(controller)
    _advance(controller, clock)
    text = await session.execute("show interfaces counters Bogus0/0/0")
    assert "Unknown interface" in text and "InOctets" not in text


@pytest.mark.asyncio
async def test_huawei_display_interface_rates_and_utility(tmp_path, raw_lab, clock):
    controller, session = _brand_session(tmp_path, raw_lab, clock, "huawei/vrp/s5735")
    try:
        assert session.family == "vrp"
        _advance(controller, clock)
        detail = await session.execute("display interface GigabitEthernet1/0/1")
        assert f"Last 300 seconds input rate {FLOW_BPS} bits/sec, 1000 packets/sec" in detail
        assert f"Input:  {PACKETS} packets, {BYTES} bytes" in detail
        table = await session.execute("display interface counters")
        assert "InOctets" in table and str(BYTES) in table
        brief = await session.execute("display interface brief")
        assert "1%" in brief and "0%" in brief
    finally:
        controller.store.close()


@pytest.mark.asyncio
async def test_junos_statistics_keyword_maps_to_counters(tmp_path, raw_lab, clock):
    controller, session = _brand_session(tmp_path, raw_lab, clock, "juniper/junos/demo")
    try:
        assert session.family == "junos"
        _advance(controller, clock)
        table = await session.execute("show interfaces statistics")
        assert "In bytes" in table and str(BYTES) in table
    finally:
        controller.store.close()


@pytest.mark.asyncio
async def test_brand_gating_and_counter_reset(controller, clock):
    session, client = _session(controller)
    _advance(controller, clock)
    assert "Unknown query for this brand" in await session.execute("display interface counters")
    before = await session.execute("show interfaces counters")
    assert str(BYTES) in before
    await client.transact([{"kind": "counter_reset"}])
    after = await session.execute("show interfaces counters")
    assert str(BYTES) not in after
    snap = controller.snapshot("sw-a")
    rates = counter_rates(snap["runtime"]["counters"]["p1"])
    assert rates["in_bps"] == FLOW_BPS and rates["in_pps"] == 1000


def test_utility_is_rounded_and_guards_zero_speed():
    assert util_percent(8_000_000, 1_000_000_000) == 1
    assert util_percent(0, 1_000_000_000) == 0
    assert util_percent(5_000_000_000, 10_000_000_000) == 50
    assert util_percent(8_000_000, 0) == 0


def test_counter_table_keeps_columns_aligned_for_long_names():
    device = {"interfaces": {
        "p1": {"id": "p1", "name": "HundredGigE1/0/25:1", "speed_bps": 100_000_000_000},
        "p2": {"id": "p2", "name": "Bundle-Ether1000", "speed_bps": 100_000_000_000}}}
    counters = {"p1": {"in_bytes": 1234567890}, "p2": {"in_bytes": 9876543210}}
    header, first, second = render_counters(device, counters, "ios").splitlines()
    assert first.startswith("Hu1/0/25:1")
    assert first.index("1234567890") == header.index("InOctets")
    assert second.index("9876543210") == header.index("InOctets")
