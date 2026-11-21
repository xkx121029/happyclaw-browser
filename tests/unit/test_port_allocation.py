"""CDP port assignment: pick a port nothing else on the machine owns.

Two HappyClaw threads on one machine used to collide on 9222 and Chrome
exited with an opaque bind error. These tests hold a real socket open and
assert the profile moves off it — and that the port it reports is the one it
actually got.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from happyclaw_browser.profile import ProfileManager, port_available, reassign_port
from happyclaw_browser.settings import HappyclawSettings


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Occupied:
    """Hold a port the way another thread's Chrome would."""

    def __init__(self, port: int) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", port))
        self._socket.listen(1)
        self.port = port

    def release(self) -> None:
        self._socket.close()


def _settings(port: int, *, scan: int = 20) -> HappyclawSettings:
    return HappyclawSettings(
        cdp_port_start=port,
        cdp_port_scan_limit=scan,
        cdp_host="localhost",
    )


@pytest.fixture()
def occupied():
    port = _free_port()
    holder = _Occupied(port)
    try:
        yield holder
    finally:
        holder.release()


def test_port_available_sees_a_bound_port():
    port = _free_port()
    assert port_available(port) is True
    holder = _Occupied(port)
    try:
        assert port_available(port) is False
    finally:
        holder.release()


def test_profile_moves_off_a_port_another_process_owns(tmp_path, occupied):
    manager = ProfileManager(
        base_dir=tmp_path / "profiles", settings=_settings(occupied.port)
    )

    profile = manager.get_or_create("work")

    assert profile.cdp_port != occupied.port
    assert port_available(profile.cdp_port), "the reported port must be bindable"
    assert manager.assigned_port("work") == profile.cdp_port


def test_two_profiles_never_share_a_port(tmp_path):
    manager = ProfileManager(
        base_dir=tmp_path / "profiles", settings=_settings(_free_port())
    )

    first = manager.get_or_create("one")
    second = manager.get_or_create("two")

    assert first.cdp_port != second.cdp_port


def test_recorded_port_survives_a_new_process(tmp_path):
    base = tmp_path / "profiles"
    start = _free_port()
    first = ProfileManager(base_dir=base, settings=_settings(start))
    assigned = first.get_or_create("work").cdp_port

    second = ProfileManager(base_dir=base, settings=_settings(start))

    assert second.get_or_create("work").cdp_port == assigned


def test_exhausted_scan_window_says_what_to_change(tmp_path, occupied):
    manager = ProfileManager(
        base_dir=tmp_path / "profiles",
        settings=_settings(occupied.port, scan=1),
    )

    with pytest.raises(RuntimeError, match="No free CDP port"):
        manager.get_or_create("work")


def test_reassign_port_moves_a_profile_and_persists_it(tmp_path, occupied):
    base = tmp_path / "profiles"
    manager = ProfileManager(base_dir=base, settings=_settings(_free_port()))
    profile = manager.get_or_create("work")
    settings = _settings(occupied.port)

    new_port = reassign_port(profile, settings)

    assert new_port is not None
    assert new_port != occupied.port
    assert profile.cdp_port == new_port
    assert port_available(new_port)
    reloaded = ProfileManager(base_dir=base, settings=settings)
    assert reloaded.get_or_create("work").cdp_port == new_port


def test_each_profile_gets_its_own_user_data_dir(tmp_path):
    manager = ProfileManager(
        base_dir=tmp_path / "profiles", settings=_settings(_free_port())
    )

    profile = manager.get_or_create("solo")

    assert profile.data_dir == Path(tmp_path / "profiles" / "solo")
    assert profile.data_dir.is_dir()
    assert profile.fresh is True
    assert manager.get_or_create("solo").fresh is True
    assert (
        ProfileManager(base_dir=tmp_path / "profiles", settings=_settings(_free_port()))
        .get_or_create("solo")
        .fresh
        is False
    )
