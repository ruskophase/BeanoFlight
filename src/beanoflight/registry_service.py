"""Command-line BeanRegistry service for live multi-process deployments."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import signal
import socket
import stat
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

from .registry import BeanRegistry
from .registry_sqlite import SQLiteBeanRepository
from .registry_zmq import ZeroMQRegistryServer

DEFAULT_COMMAND_ENDPOINT = "ipc:///tmp/beanoflight-registry-commands.ipc"
DEFAULT_EVENT_ENDPOINT = "ipc:///tmp/beanoflight-registry-events.ipc"


class RegistryInstanceError(RuntimeError):
    """Another process owns the database or one of the service endpoints."""


class RegistryInstanceGuard:
    """Hold advisory locks for one database writer and its advertised endpoints."""

    def __init__(
        self, database: Path, *, command_endpoint: str, event_endpoint: str
    ) -> None:
        self.database = database.expanduser().resolve()
        self.command_endpoint = command_endpoint
        self.event_endpoint = event_endpoint
        self._files: list[tuple[Path, int]] = []

    def __enter__(self) -> RegistryInstanceGuard:  # noqa: PYI034 - Python 3.10
        lock_paths = {
            self.database.with_name(self.database.name + ".registry.lock"),
            _endpoint_lock_path(self.command_endpoint),
            _endpoint_lock_path(self.event_endpoint),
        }
        try:
            for path in sorted(lock_paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    owner = _lock_owner(descriptor)
                    os.close(descriptor)
                    detail = f"; owner {owner}" if owner else ""
                    raise RegistryInstanceError(
                        f"registry ownership lock is already held: {path}{detail}"
                    ) from exc
                self._files.append((path, descriptor))
            legacy_owners = registry_processes_for_database(self.database)
            if legacy_owners:
                identifiers = ", ".join(str(pid) for pid in legacy_owners)
                raise RegistryInstanceError(
                    "database is already open by BeanRegistry process(es): "
                    f"{identifiers}"
                )
            for endpoint in (self.command_endpoint, self.event_endpoint):
                if ipc_endpoint_has_listener(endpoint):
                    raise RegistryInstanceError(
                        f"registry endpoint already has a listener: {endpoint}"
                    )
            metadata = json.dumps(
                {
                    "pid": os.getpid(),
                    "database": str(self.database),
                    "commands": self.command_endpoint,
                    "events": self.event_endpoint,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            for _path, descriptor in self._files:
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, metadata)
                os.fsync(descriptor)
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def close(self) -> None:
        for _path, descriptor in reversed(self._files):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        self._files.clear()


def ipc_endpoint_has_listener(endpoint: str, *, timeout_seconds: float = 0.1) -> bool:
    """Return true when a filesystem IPC endpoint is occupied by a listener."""

    path = _ipc_endpoint_path(endpoint)
    if path is None:
        return False
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISSOCK(mode):
        return True
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(max(0.001, float(timeout_seconds)))
    try:
        probe.connect(str(path))
    except OSError as exc:
        return exc.errno not in {errno.ENOENT, errno.ECONNREFUSED}
    else:
        return True
    finally:
        probe.close()


def registry_processes_for_database(
    database: Path, *, exclude_pid: int | None = None
) -> tuple[int, ...]:
    """Find pre-lock BeanRegistry processes which opened the selected database."""

    expected = database.expanduser().resolve()
    ignored = os.getpid() if exclude_pid is None else int(exclude_pid)
    owners: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return ()
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == ignored:
            continue
        try:
            encoded = (entry / "cmdline").read_bytes()
            arguments = tuple(
                item.decode("utf-8", errors="surrogateescape")
                for item in encoded.split(b"\0")
                if item
            )
            if not _is_registry_command(arguments):
                continue
            state = (entry / "stat").read_text(encoding="utf-8").split()[2]
            if state == "Z":
                continue
            cwd = Path(os.readlink(entry / "cwd"))
            candidate = _registry_database_argument(arguments)
            candidate = candidate if candidate.is_absolute() else cwd / candidate
            if (
                candidate.expanduser().resolve() == expected
                and _process_has_open_database(entry, expected)
            ):
                owners.append(int(entry.name))
        except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
            continue
    return tuple(sorted(owners))


def _process_has_open_database(process: Path, database: Path) -> bool:
    try:
        descriptors = tuple((process / "fd").iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return False
    for descriptor in descriptors:
        try:
            target = Path(os.readlink(descriptor))
            if target.is_absolute() and target.resolve() == database:
                return True
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return False


def _is_registry_command(arguments: tuple[str, ...]) -> bool:
    return bool(arguments) and (
        "beanoflight.registry_service" in arguments
        or Path(arguments[0]).name in {"beano-registry", "beanoflight.registry_service"}
    )


def _registry_database_argument(arguments: tuple[str, ...]) -> Path:
    for index, argument in enumerate(arguments):
        if argument == "--database" and index + 1 < len(arguments):
            return Path(arguments[index + 1])
        if argument.startswith("--database="):
            return Path(argument.partition("=")[2])
    return Path("beanoflight.db")


def _ipc_endpoint_path(endpoint: str) -> Path | None:
    prefix = "ipc://"
    if not endpoint.startswith(prefix):
        return None
    value = endpoint[len(prefix) :]
    if not value or value.startswith("@") or value == "*":
        return None
    return Path(value).expanduser().resolve()


def _endpoint_lock_path(endpoint: str) -> Path:
    ipc_path = _ipc_endpoint_path(endpoint)
    if ipc_path is not None:
        return ipc_path.with_name(ipc_path.name + ".registry.lock")
    # Non-filesystem transports are uncommon here. Keep their ownership lock
    # beside the database rather than pretending the transport has no owner.
    digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"beanoflight-endpoint-{digest}.registry.lock"


def _lock_owner(descriptor: int) -> str:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        return os.read(descriptor, 4_096).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="beano-registry",
        description="BeanoFlight authoritative bean-state registry",
    )
    result.add_argument(
        "--database",
        type=Path,
        default=Path("beanoflight.db"),
        help="local SQLite history database (default: ./beanoflight.db)",
    )
    result.add_argument(
        "--commands",
        default=DEFAULT_COMMAND_ENDPOINT,
        help=f"ZeroMQ command/query endpoint (default: {DEFAULT_COMMAND_ENDPOINT})",
    )
    result.add_argument(
        "--events",
        default=DEFAULT_EVENT_ENDPOINT,
        help=f"ZeroMQ event endpoint (default: {DEFAULT_EVENT_ENDPOINT})",
    )
    result.add_argument(
        "--busy-timeout-ms",
        type=int,
        default=2_000,
        help="SQLite lock wait limit in milliseconds",
    )
    result.add_argument(
        "--log-track-updates",
        action="store_true",
        help="also print high-volume per-frame track.updated activity",
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    stop = threading.Event()

    def request_stop(_signal_number, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        with (
            RegistryInstanceGuard(
                arguments.database,
                command_endpoint=arguments.commands,
                event_endpoint=arguments.events,
            ),
            SQLiteBeanRepository(
                arguments.database, busy_timeout_ms=arguments.busy_timeout_ms
            ) as repository,
        ):
            registry = BeanRegistry(repository)
            server = ZeroMQRegistryServer(
                registry,
                command_endpoint=arguments.commands,
                event_endpoint=arguments.events,
                event_observer=lambda event: _print_event(
                    event, include_track_updates=arguments.log_track_updates
                ),
            )
            print(f"BeanRegistry database: {repository.path}", flush=True)
            print(f"BeanRegistry commands: {arguments.commands}", flush=True)
            print(f"BeanRegistry events: {arguments.events}", flush=True)
            server.serve_forever(stop)
    except RegistryInstanceError as exc:
        print(f"BeanRegistry refused to start: {exc}", file=sys.stderr, flush=True)
        return 2
    return 0


def _print_event(event, *, include_track_updates: bool) -> None:
    if event.kind == "track.updated" and not include_track_updates:
        return
    print(
        f"#{event.stream_sequence:06d} {event.kind:24} "
        f"{event.bean_ref} revision={event.revision}",
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
