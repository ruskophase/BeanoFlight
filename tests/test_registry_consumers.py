import threading
import unittest
from unittest.mock import patch

from test_registry import track

from beanoflight.models import BeanEvent, BeanRef
from beanoflight.registry import BeanRegistry
from beanoflight.registry_models import RunSession, RunState
from beanoflight.registry_monitor import RegistryMonitorWorker
from beanoflight.sorter import SorterService, _recovery_run_ids


def session(run_id: str, state: RunState, updated_timestamp_ns: int) -> RunSession:
    return RunSession(
        run_id,
        1,
        state,
        f"/{run_id}",
        "raw-mmap-green",
        100,
        60.0,
        60.0,
        100,
        100,
        1_000,
        False,
        updated_timestamp_ns - 1,
        updated_timestamp_ns,
        {},
    )


class FakeConsumerClient:
    def __init__(self, sessions, record, events):
        self.sessions = sessions
        self.record = record
        self.events = events
        self.list_run_ids = []
        self.get_calls = []

    def ping(self):
        return {"service": "BeanRegistry"}

    def event_cursor(self):
        return 100

    def list_sessions(self):
        return self.sessions

    def list_records(self, *, run_id=None):
        self.list_run_ids.append(run_id)
        return (self.record,) if run_id == self.record.bean_ref.run_id else ()

    def events_since(self, _cursor, *, limit=1_000):
        events, self.events = self.events, ()
        return events[:limit]

    def events_since_compact(self, cursor, *, limit=1_000):
        return self.events_since(cursor, limit=limit)

    def get(self, bean_ref, *, include_history=True):
        self.get_calls.append((bean_ref, include_history))
        return self.record

    def close(self):
        pass


class RegistryConsumerTests(unittest.TestCase):
    def setUp(self):
        self.bean_ref = BeanRef("current-run", 1)
        self.record = BeanRegistry().update_track(
            track(self.bean_ref, 0, 100, -25.0), event_id="track"
        )
        self.events = (
            BeanEvent(
                "track.updated",
                self.bean_ref,
                101,
                revision=2,
                event_id="event-101",
                stream_sequence=101,
            ),
            BeanEvent(
                "inference.completed",
                self.bean_ref,
                102,
                revision=3,
                event_id="event-102",
                stream_sequence=102,
            ),
        )

    def test_sorter_recovers_only_live_and_latest_runs_and_coalesces_events(self):
        sessions = (
            session("old-run", RunState.COMPLETED, 10),
            session("paused-run", RunState.PAUSED, 20),
            session("latest-run", RunState.COMPLETED, 30),
        )
        self.assertEqual(_recovery_run_ids(sessions), ("paused-run", "latest-run"))
        recovery_client = FakeConsumerClient(
            (
                session("old-run", RunState.COMPLETED, 10),
                session("current-run", RunState.RUNNING, 20),
            ),
            self.record,
            (),
        )
        sorter = SorterService()
        sorter._recover_current_state(recovery_client)
        self.assertEqual(sorter._cursor, 100)
        self.assertEqual(recovery_client.list_run_ids, ["current-run"])

        client = FakeConsumerClient((), self.record, self.events)
        sorter._process_events(self.events, client)
        self.assertEqual(
            client.get_calls,
            [(self.bean_ref, False)],
        )
        self.assertEqual(sorter._cursor, 102)

    def test_monitor_snapshots_only_latest_run_and_updates_each_bean_once(self):
        sessions = (
            session("old-run", RunState.COMPLETED, 10),
            session("current-run", RunState.RUNNING, 20),
        )
        client = FakeConsumerClient(sessions, self.record, self.events)
        snapshots = []
        ready = threading.Event()

        def callback(snapshot):
            snapshots.append(snapshot)
            ready.set()

        with patch(
            "beanoflight.registry_monitor.ZeroMQRegistryClient",
            return_value=client,
        ):
            worker = RegistryMonitorWorker(callback, refresh_seconds=10.0)
            worker.start()
            self.assertTrue(ready.wait(2.0))
            worker.close()

        self.assertEqual(client.list_run_ids, ["current-run"])
        self.assertEqual(client.get_calls, [(self.bean_ref, False)])
        self.assertEqual(snapshots[-1].records, (self.record,))
        self.assertEqual(snapshots[-1].cursor, 102)
        self.assertEqual(
            tuple(event.kind for event in snapshots[-1].significant_events),
            ("inference.completed",),
        )

    def test_sorter_fetches_direct_enrichment_events(self):
        event = BeanEvent(
            "enrichment.added",
            self.bean_ref,
            103,
            revision=3,
            event_id="event-103",
            stream_sequence=103,
        )
        client = FakeConsumerClient((), self.record, (event,))

        SorterService()._process_events((event,), client)

        self.assertEqual(client.get_calls, [(self.bean_ref, False)])


if __name__ == "__main__":
    unittest.main()
