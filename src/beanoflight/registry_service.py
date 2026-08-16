"""Command-line BeanRegistry service for live multi-process deployments."""

from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Sequence
from pathlib import Path

from .registry import BeanRegistry
from .registry_sqlite import SQLiteBeanRepository
from .registry_zmq import ZeroMQRegistryServer

DEFAULT_COMMAND_ENDPOINT = "ipc:///tmp/beanoflight-registry-commands.ipc"
DEFAULT_EVENT_ENDPOINT = "ipc:///tmp/beanoflight-registry-events.ipc"


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
    with SQLiteBeanRepository(
        arguments.database, busy_timeout_ms=arguments.busy_timeout_ms
    ) as repository:
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
