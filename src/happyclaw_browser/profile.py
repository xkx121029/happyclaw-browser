"""Profile management for browser session persistence."""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from happyclaw_browser.settings import settings as _settings

if TYPE_CHECKING:
    from happyclaw_browser.settings import HappyclawSettings

logger = logging.getLogger(__name__)

# State file inside the profile dir that pins the page-level CDP target the
# *previous* happyclaw-browser session attached to. CLI invocations are
# short-lived (one process per command) so without this file each new process
# would attach to whatever target Chrome happens to list first — which can be
# a stale ``about:blank`` tab rather than the one the user was driving.
_TARGET_STATE_FILE = ".happyclaw-browser-target.json"
_PROFILE_PORTS_FILE = ".happyclaw-browser-profiles.json"
# Pid of the Chrome this library launched for the profile, used to stop an
# instance that ignores Browser.close.
_PID_STATE_FILE = ".happyclaw-browser-chrome.pid"

# Older alternate filenames, read as a fallback so an existing profile keeps
# its port assignment, target binding and launched-pid record instead of
# re-attaching to a random tab or losing the process it can still stop.
_LEGACY_TARGET_STATE_FILES = (
    ".octop-browser-target.json",
    ".harness-browser-target.json",
)
_LEGACY_PROFILE_PORTS_FILES = (
    ".octop-browser-profiles.json",
    ".harness-browser-profiles.json",
)
_LEGACY_PID_STATE_FILES = (".octop-browser-chrome.pid",)


def _readable_state_file(directory: Path, name: str, legacy: tuple[str, ...]) -> Path:
    """Return the first of ``name`` / ``legacy`` that exists under ``directory``."""
    path = directory / name
    if path.is_file():
        return path
    for old in legacy:
        candidate = directory / old
        if candidate.is_file():
            return candidate
    return path


def _probe_host(cdp_host: str) -> str:
    """Map a configured CDP host onto an address a socket can bind."""
    if cdp_host in {"", "localhost", "127.0.0.1"}:
        return "127.0.0.1"
    return cdp_host


def port_available(port: int, cdp_host: str = "localhost") -> bool:
    """Return True when *port* can be bound on *cdp_host* right now.

    Chrome cannot bind ``--remote-debugging-port`` on a port another process
    already owns, and the launcher only reports that as an opaque "Chrome
    exited immediately". Probing before assignment turns a shared machine —
    several HappyClaw threads, or a leftover browser from a previous run —
    into a launch on a neighbouring port instead of a hard failure.
    """
    if not 1 <= port <= 65535:
        return False
    address = _probe_host(cdp_host)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        probe = socket.socket(family, socket.SOCK_STREAM)
    except OSError:  # pragma: no cover - address family unavailable
        return False
    try:
        probe.bind((address, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def process_alive(pid: int) -> bool:
    """Best-effort liveness probe for a recorded Chrome pid."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(  # noqa: S603
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _first_free_port(
    *,
    start: int,
    limit: int,
    cdp_host: str,
    reserved: set[int],
) -> int | None:
    """Return the first bindable port in ``[start, start + limit)``, or None."""
    for offset in range(max(int(limit), 1)):
        port = max(int(start), 1) + offset
        if port in reserved or not port_available(port, cdp_host):
            continue
        return port
    return None


def _read_ports_file(base_dir: Path) -> dict[str, int]:
    path = _readable_state_file(
        base_dir, _PROFILE_PORTS_FILE, _LEGACY_PROFILE_PORTS_FILES
    )
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    ports: dict[str, int] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, int):
            ports[key] = value
    return ports


def _write_ports_file(base_dir: Path, ports: dict[str, int]) -> None:
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / _PROFILE_PORTS_FILE).write_text(
            json.dumps(ports, sort_keys=True), encoding="utf-8"
        )
    except OSError as exc:
        logger.debug("Could not write port map under %s: %s", base_dir, exc)


def reassign_port(profile: Profile, cfg: HappyclawSettings) -> int | None:
    """Move *profile* to a free CDP port and persist the new mapping.

    The launcher calls this when the profile's assigned port is owned by a
    process that is not this profile's Chrome (another HappyClaw thread, a
    browser left over from an earlier run, an unrelated daemon). Updating
    ``profile.cdp_port`` in place is what makes the *actual* port visible to
    every caller that already holds the profile object.

    Returns the new port, or ``None`` when the whole scan window is taken.
    """
    base_dir = Path(profile.data_dir).parent
    ports = _read_ports_file(base_dir)
    reserved = {p for name, p in ports.items() if name != profile.name}
    port = _first_free_port(
        start=cfg.cdp_port_start,
        limit=cfg.cdp_port_scan_limit,
        cdp_host=cfg.cdp_host,
        reserved=reserved,
    )
    if port is None:
        return None
    ports[profile.name] = port
    _write_ports_file(base_dir, ports)
    previous = profile.cdp_port
    profile.cdp_port = port
    if previous != port:
        # The cached target id belongs to the browser that owned the old
        # port; a fresh instance on a fresh port cannot resolve it.
        profile.clear_target()
        logger.warning(
            "CDP port %d for profile %r is taken; using %d instead",
            previous,
            profile.name,
            port,
        )
    return port


@dataclass
class Profile:
    """A named browser profile backed by a Chrome user-data-dir."""

    name: str
    data_dir: Path
    cdp_port: int

    #: True while this profile has never been launched successfully. A failed
    #: first launch leaves a half-initialised user-data-dir (partial ``Local
    #: State``, ``SingletonLock``) that makes the *next* launch exit 21, so the
    #: launcher removes the directory instead of keeping the wreckage.
    fresh: bool = False

    # ------------------------------------------------------------------
    # Cross-process target stickiness
    # ------------------------------------------------------------------

    @property
    def _target_state_path(self) -> Path:
        return self.data_dir / _TARGET_STATE_FILE

    def load_target(self) -> str | None:
        """Return the cached page-level CDP target id, or ``None``.

        Used by :func:`happyclaw_browser.cdp.launcher.get_page_ws_url` to
        re-attach to the same tab across CLI invocations.
        """
        path = _readable_state_file(
            self.data_dir, _TARGET_STATE_FILE, _LEGACY_TARGET_STATE_FILES
        )
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Could not read %s: %s", path, exc)
            return None
        target_id = payload.get("target_id")
        return target_id if isinstance(target_id, str) and target_id else None

    def save_target(self, target_id: str) -> None:
        """Persist the active page-level target id.

        Best-effort: any I/O error is swallowed (next attach falls back to
        the heuristic in ``get_page_ws_url``).
        """
        if not target_id:
            return
        path = self._target_state_path
        try:
            path.write_text(
                json.dumps({"target_id": target_id}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("Could not write %s: %s", path, exc)

    def clear_target(self) -> None:
        """Forget the cached target id (best-effort)."""
        for name in (_TARGET_STATE_FILE, *_LEGACY_TARGET_STATE_FILES):
            path = self.data_dir / name
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Could not unlink %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Launched-process tracking
    # ------------------------------------------------------------------

    @property
    def _pid_path(self) -> Path:
        return Path(self.data_dir) / _PID_STATE_FILE

    def save_pid(self, pid: int) -> None:
        """Remember the Chrome we launched for this profile.

        ``terminate_browser`` needs the pid to stop a browser that ignores
        ``Browser.close``; on Windows there is no other way to reach a
        specific instance, since the POSIX ``pkill --user-data-dir`` pattern
        does not exist there.
        """
        if pid <= 0:
            return
        try:
            self._pid_path.write_text(str(pid), encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not write %s: %s", self._pid_path, exc)

    def load_pid(self) -> int | None:
        """Return the pid recorded for this profile, or ``None``."""
        path = _readable_state_file(
            Path(self.data_dir), _PID_STATE_FILE, _LEGACY_PID_STATE_FILES
        )
        try:
            raw = path.read_text(encoding="utf-8").strip()
            pid = int(raw)
        except (OSError, ValueError):
            return None
        return pid if pid > 0 else None

    def clear_pid(self) -> None:
        """Forget the recorded pid (best-effort)."""
        for name in (_PID_STATE_FILE, *_LEGACY_PID_STATE_FILES):
            path = Path(self.data_dir) / name
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Could not unlink %s: %s", path, exc)


class ProfileManager:
    """Creates and tracks browser profiles with stable CDP ports."""

    def __init__(
        self,
        base_dir: Path | None = None,
        settings: HappyclawSettings | None = None,
    ) -> None:
        """Create a manager.

        Args:
            base_dir: Root for profile user-data-dirs. Defaults to
                ``settings.profiles_dir``.
            settings: Settings used for port allocation. Defaults to the
                process-wide singleton. Pass an explicit object to keep a
                manager fully independent of the ambient environment —
                otherwise ``cdp_port_start`` would still come from the global
                settings even when ``base_dir`` is overridden.
        """
        cfg = settings or _settings
        self._settings = cfg
        self._base_dir = base_dir if base_dir is not None else cfg.profiles_dir
        self._profiles: dict[str, Profile] = {}

    def _load_ports(self) -> dict[str, int]:
        return _read_ports_file(self._base_dir)

    def _save_ports(self, ports: dict[str, int]) -> None:
        _write_ports_file(self._base_dir, ports)

    def _assign_port(self, name: str) -> int:
        """Return the CDP port for *name*, steering clear of ports in use.

        A profile keeps its recorded port as long as that port is bindable, or
        as long as the Chrome this library launched for it is still alive —
        re-attaching to a running browser must not shuffle ports. Anything
        else (a sibling thread's browser, an unrelated listener) forces a scan
        for the next free port.
        """
        ports = self._load_ports()
        reserved = set(ports.values()) | {p.cdp_port for p in self._profiles.values()}
        existing = ports.get(name)
        if existing is not None and self._port_reusable(name, existing):
            return existing

        port = _first_free_port(
            start=self._settings.cdp_port_start,
            limit=self._settings.cdp_port_scan_limit,
            cdp_host=self._settings.cdp_host,
            reserved=reserved,
        )
        scan_end = (
            self._settings.cdp_port_start + self._settings.cdp_port_scan_limit - 1
        )
        if port is None:
            raise RuntimeError(
                f"No free CDP port in {self._settings.cdp_port_start}-{scan_end} "
                f"on {self._settings.cdp_host}. Raise BROWSER_USE_CDP_PORT_START "
                "or BROWSER_USE_CDP_PORT_SCAN."
            )
        ports[name] = port
        self._save_ports(ports)
        return port

    def _port_reusable(self, name: str, port: int) -> bool:
        """True when *port* is still this profile's own CDP endpoint."""
        if port_available(port, self._settings.cdp_host):
            return True
        recorded = Profile(
            name=name, data_dir=self._base_dir / name, cdp_port=port
        ).load_pid()
        return recorded is not None and process_alive(recorded)

    def get_or_create(self, name: str) -> Profile:
        """Return an existing profile or create a new one."""
        if name in self._profiles:
            return self._profiles[name]
        data_dir = self._base_dir / name
        fresh = not data_dir.exists()
        data_dir.mkdir(parents=True, exist_ok=True)
        port = self._assign_port(name)
        profile = Profile(name=name, data_dir=data_dir, cdp_port=port, fresh=fresh)
        self._profiles[name] = profile
        return profile

    def assigned_port(self, name: str) -> int:
        """Return the CDP port *name* is (or will be) listening on."""
        return self.get_or_create(name).cdp_port

    def list_profiles(self) -> list[Profile]:
        """Return all known profiles."""
        return list(self._profiles.values())

    def delete(self, name: str) -> None:
        """Remove a profile and its data directory."""
        if name not in self._profiles:
            return
        profile = self._profiles.pop(name)
        ports = self._load_ports()
        if name in ports:
            ports.pop(name, None)
            self._save_ports(ports)
        if profile.data_dir.exists():
            shutil.rmtree(profile.data_dir)
