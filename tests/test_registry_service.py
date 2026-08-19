import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from beanoflight.registry import BeanRegistry
from beanoflight.registry_service import (
    RegistryInstanceError,
    RegistryInstanceGuard,
    ipc_endpoint_has_listener,
    registry_processes_for_database,
)
from beanoflight.registry_sqlite import SQLiteBeanRepository
from beanoflight.registry_zmq import ZeroMQRegistryClient, ZeroMQRegistryServer
from beanoflight.simulation_launcher_app import (
    REGISTRY_ABSENT,
    REGISTRY_CONFLICT,
    REGISTRY_HEALTHY,
    REGISTRY_LEGACY,
    REGISTRY_UNRESPONSIVE,
    registry_endpoint_state,
)


class RegistryServiceOwnershipTests(unittest.TestCase):
    def test_launcher_identifies_registry_without_capability_metadata(self):
        class LegacyClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def ping(self):
                return {"service": "BeanRegistry", "database": ""}

            def close(self):
                pass

        with patch(
            "beanoflight.simulation_launcher_app.ZeroMQRegistryClient",
            LegacyClient,
        ):
            self.assertEqual(
                registry_endpoint_state("ipc:///unused", timeout_ms=25),
                REGISTRY_LEGACY,
            )

    def test_second_service_process_is_refused_for_the_same_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "registry.db"
            first_commands = f"ipc://{root}/first-commands.sock"
            first = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "beanoflight.registry_service",
                    "--database",
                    str(database),
                    "--commands",
                    first_commands,
                    "--events",
                    f"ipc://{root}/first-events.sock",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            client = ZeroMQRegistryClient(first_commands, timeout_ms=100)
            try:
                for _attempt in range(50):
                    try:
                        if client.ping().get("service") == "BeanRegistry":
                            break
                    except Exception:  # noqa: BLE001 - retry service startup
                        time.sleep(0.02)
                else:
                    self.fail("first registry did not become ready")
                second = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "beanoflight.registry_service",
                        "--database",
                        str(database),
                        "--commands",
                        f"ipc://{root}/second-commands.sock",
                        "--events",
                        f"ipc://{root}/second-events.sock",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
                self.assertEqual(second.returncode, 2)
                self.assertIn("refused to start", second.stderr)
            finally:
                client.close()
                first.terminate()
                first.wait(timeout=2.0)

    @unittest.skipUnless(Path("/proc").is_dir(), "legacy owner scan requires procfs")
    def test_detects_a_pre_lock_registry_database_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "exec 9<> beanoflight.db; exec -a beano-registry sleep 30",
                ],
                cwd=root,
            )
            try:
                database = root / "beanoflight.db"
                owners = ()
                for _attempt in range(50):
                    owners = registry_processes_for_database(database)
                    if process.pid in owners:
                        break
                    time.sleep(0.01)
                self.assertIn(process.pid, owners)
                self.assertEqual(
                    registry_endpoint_state(
                        f"ipc://{root}/stale.sock",
                        database=database,
                        timeout_ms=25,
                    ),
                    REGISTRY_UNRESPONSIVE,
                )
            finally:
                process.terminate()
                process.wait(timeout=2.0)

    def test_guard_exclusively_owns_database_and_endpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "registry.db"
            commands = f"ipc://{root}/commands.sock"
            events = f"ipc://{root}/events.sock"
            with (
                RegistryInstanceGuard(
                    database, command_endpoint=commands, event_endpoint=events
                ),
                self.assertRaises(RegistryInstanceError),
                RegistryInstanceGuard(
                    database, command_endpoint=commands, event_endpoint=events
                ),
            ):
                pass
            with RegistryInstanceGuard(
                database, command_endpoint=commands, event_endpoint=events
            ):
                pass

    def test_guard_rejects_legacy_listener_without_a_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            endpoint_path = root / "commands.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(endpoint_path))
            listener.listen(4)
            try:
                endpoint = f"ipc://{endpoint_path}"
                self.assertTrue(ipc_endpoint_has_listener(endpoint))
                with (
                    self.assertRaisesRegex(
                        RegistryInstanceError, "already has a listener"
                    ),
                    RegistryInstanceGuard(
                        root / "registry.db",
                        command_endpoint=endpoint,
                        event_endpoint=f"ipc://{root}/events.sock",
                    ),
                ):
                    pass
            finally:
                listener.close()
            self.assertFalse(ipc_endpoint_has_listener(f"ipc://{endpoint_path}"))

    def test_launcher_distinguishes_absent_healthy_and_unresponsive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            absent = f"ipc://{root}/absent.sock"
            self.assertEqual(
                registry_endpoint_state(absent, timeout_ms=25), REGISTRY_ABSENT
            )

            occupied_path = root / "occupied.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(occupied_path))
            listener.listen(4)
            try:
                self.assertEqual(
                    registry_endpoint_state(f"ipc://{occupied_path}", timeout_ms=25),
                    REGISTRY_UNRESPONSIVE,
                )
            finally:
                listener.close()

            repository = SQLiteBeanRepository(root / "healthy.db")
            registry = BeanRegistry(repository)
            command_endpoint = f"ipc://{root}/healthy-commands.sock"
            server = ZeroMQRegistryServer(
                registry,
                command_endpoint=command_endpoint,
                event_endpoint=f"ipc://{root}/healthy-events.sock",
            )
            stop = threading.Event()
            ready = threading.Event()
            worker = threading.Thread(
                target=server.serve_forever,
                args=(stop,),
                kwargs={"ready": ready},
                daemon=True,
            )
            worker.start()
            self.assertTrue(ready.wait(2.0))
            try:
                self.assertEqual(
                    registry_endpoint_state(
                        command_endpoint,
                        database=root / "healthy.db",
                        timeout_ms=500,
                    ),
                    REGISTRY_HEALTHY,
                )
                self.assertEqual(
                    registry_endpoint_state(
                        command_endpoint,
                        database=root / "other.db",
                        timeout_ms=500,
                    ),
                    REGISTRY_CONFLICT,
                )
            finally:
                stop.set()
                worker.join(2.0)
                repository.close()


if __name__ == "__main__":
    unittest.main()
