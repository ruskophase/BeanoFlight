"""SQLite WAL repository for BeanRegistry current state and event history."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .models import (
    BeanEvent,
    BeanRef,
    Detection,
    Observation,
    TrackSnapshot,
    TrackStatus,
)
from .registry_models import (
    BeanRecord,
    decision_from_dict,
    enrichment_from_dict,
    enrichment_to_dict,
    event_from_dict,
    prediction_from_dict,
    prediction_to_dict,
)

SCHEMA_VERSION = 1
TRACK_EVENT_KINDS = {
    "bean.created",
    "bean.confirmed",
    "track.updated",
    "bean.exited",
    "bean.cancelled",
}


class SQLiteBeanRepository:
    """Single-connection repository safe for calls from multiple local threads."""

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 2_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=max(0.001, busy_timeout_ms / 1_000.0),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._batch_depth = 0
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                self._connection.close()
                raise RuntimeError(f"could not enable SQLite WAL mode: {mode}")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()

    @property
    def journal_mode(self) -> str:
        with self._lock:
            return str(
                self._connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()

    def save(self, record: BeanRecord, event: BeanEvent) -> int:
        with self._lock:
            if self._batch_depth:
                return self._save_locked(record, event)
            with self._connection:
                return self._save_locked(record, event)

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Commit several registry events as one SQLite transaction."""

        with self._lock:
            outermost = self._batch_depth == 0
            if outermost:
                self._connection.execute("BEGIN")
            self._batch_depth += 1
            try:
                yield
            except Exception:
                self._batch_depth -= 1
                if outermost:
                    self._connection.rollback()
                raise
            else:
                self._batch_depth -= 1
                if outermost:
                    self._connection.commit()

    def _save_locked(self, record: BeanRecord, event: BeanEvent) -> int:
        if record.bean_ref != event.bean_ref or record.revision != event.revision:
            raise ValueError("event does not describe the supplied bean revision")
        ref = record.bean_ref
        self._connection.execute(
            """
            INSERT INTO sessions(run_id, created_timestamp_ns)
            VALUES (?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                created_timestamp_ns = MIN(created_timestamp_ns, excluded.created_timestamp_ns)
            """,
            (ref.run_id, record.created_timestamp_ns),
        )
        self._connection.execute(
            """
            INSERT INTO beans(
                run_id, sequence, revision, status,
                created_timestamp_ns, updated_timestamp_ns
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, sequence) DO UPDATE SET
                revision=excluded.revision,
                status=excluded.status,
                updated_timestamp_ns=excluded.updated_timestamp_ns
            """,
            (
                ref.run_id,
                ref.sequence,
                record.revision,
                record.status.value,
                record.created_timestamp_ns,
                record.updated_timestamp_ns,
            ),
        )
        if event.kind in TRACK_EVENT_KINDS:
            self._save_track(
                record, include_full_history=event.kind == "bean.created"
            )
        for enrichment in record.enrichments:
            value = enrichment_to_dict(enrichment)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO enrichments(
                    run_id, sequence, result_id, registry_revision,
                    source, kind, value_json, timestamp_ns, model_version, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref.run_id,
                    ref.sequence,
                    enrichment.result_id,
                    record.revision,
                    enrichment.source,
                    enrichment.kind,
                    _json(value["value"]),
                    enrichment.timestamp_ns,
                    enrichment.version,
                    enrichment.confidence,
                ),
            )
        if record.decision is not None:
            decision = record.decision
            self._connection.execute(
                """
                INSERT INTO sorting_decisions(
                    decision_id, run_id, sequence, registry_revision, source,
                    timestamp_ns, actuation_timestamp_ns, gate_indices_json,
                    policy_version, reason, acknowledged_timestamp_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    registry_revision=excluded.registry_revision,
                    acknowledged_timestamp_ns=excluded.acknowledged_timestamp_ns
                """,
                (
                    decision.decision_id,
                    ref.run_id,
                    ref.sequence,
                    record.revision,
                    decision.source,
                    decision.timestamp_ns,
                    decision.actuation_timestamp_ns,
                    _json(list(decision.gate_indices)),
                    decision.policy_version,
                    decision.reason,
                    decision.acknowledged_timestamp_ns,
                ),
            )
        cursor = self._connection.execute(
            """
            INSERT INTO registry_events(
                event_id, kind, run_id, sequence, revision,
                timestamp_ns, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.kind,
                ref.run_id,
                ref.sequence,
                event.revision,
                event.timestamp_ns,
                _json(event.payload),
            ),
        )
        return int(cursor.lastrowid)

    def load(self, bean_ref: BeanRef) -> BeanRecord | None:
        with self._lock:
            bean = self._connection.execute(
                """
                SELECT revision, status, created_timestamp_ns, updated_timestamp_ns
                FROM beans WHERE run_id=? AND sequence=?
                """,
                (bean_ref.run_id, bean_ref.sequence),
            ).fetchone()
            if bean is None:
                return None
            track = self._load_track(bean_ref)
            prediction_row = self._connection.execute(
                """
                SELECT payload_json FROM predictions
                WHERE run_id=? AND sequence=?
                  AND registry_revision=(
                      SELECT MAX(registry_revision) FROM track_states
                      WHERE run_id=? AND sequence=?
                  )
                """,
                (
                    bean_ref.run_id,
                    bean_ref.sequence,
                    bean_ref.run_id,
                    bean_ref.sequence,
                ),
            ).fetchone()
            prediction = (
                None
                if prediction_row is None
                else prediction_from_dict(json.loads(prediction_row["payload_json"]))
            )
            enrichments = tuple(
                enrichment_from_dict(
                    {
                        "source": row["source"],
                        "kind": row["kind"],
                        "value": json.loads(row["value_json"]),
                        "timestamp_ns": row["timestamp_ns"],
                        "version": row["model_version"],
                        "result_id": row["result_id"],
                        "confidence": row["confidence"],
                    }
                )
                for row in self._connection.execute(
                    """
                    SELECT * FROM enrichments WHERE run_id=? AND sequence=?
                    ORDER BY registry_revision, result_id
                    """,
                    (bean_ref.run_id, bean_ref.sequence),
                )
            )
            decision_row = self._connection.execute(
                """
                SELECT * FROM sorting_decisions WHERE run_id=? AND sequence=?
                ORDER BY registry_revision DESC LIMIT 1
                """,
                (bean_ref.run_id, bean_ref.sequence),
            ).fetchone()
            decision = (
                None
                if decision_row is None
                else decision_from_dict(
                    {
                        "decision_id": decision_row["decision_id"],
                        "source": decision_row["source"],
                        "timestamp_ns": decision_row["timestamp_ns"],
                        "actuation_timestamp_ns": decision_row[
                            "actuation_timestamp_ns"
                        ],
                        "gate_indices": json.loads(
                            decision_row["gate_indices_json"]
                        ),
                        "policy_version": decision_row["policy_version"],
                        "reason": decision_row["reason"],
                        "acknowledged_timestamp_ns": decision_row[
                            "acknowledged_timestamp_ns"
                        ],
                    }
                )
            )
            return BeanRecord(
                bean_ref=bean_ref,
                revision=int(bean["revision"]),
                status=TrackStatus(bean["status"]),
                created_timestamp_ns=int(bean["created_timestamp_ns"]),
                updated_timestamp_ns=int(bean["updated_timestamp_ns"]),
                track=track,
                prediction=prediction,
                enrichments=enrichments,
                decision=decision,
            )

    def list_records(
        self,
        *,
        run_id: str | None = None,
        statuses: Sequence[TrackStatus] | None = None,
    ) -> tuple[BeanRecord, ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        if run_id is not None:
            clauses.append("run_id=?")
            parameters.append(run_id)
        if statuses is not None:
            values = tuple(status.value for status in statuses)
            if not values:
                return ()
            clauses.append(f"status IN ({','.join('?' for _ in values)})")
            parameters.extend(values)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with self._lock:
            references = tuple(
                BeanRef(row["run_id"], int(row["sequence"]))
                for row in self._connection.execute(
                    "SELECT run_id, sequence FROM beans"
                    + where
                    + " ORDER BY run_id, sequence",
                    parameters,
                )
            )
            return tuple(
                record
                for reference in references
                if (record := self.load(reference)) is not None
            )

    def event_identity(self, event_id: str) -> tuple[BeanRef, str] | None:
        if not event_id:
            return None
        with self._lock:
            row = self._connection.execute(
                """
                SELECT run_id, sequence, payload_json
                FROM registry_events WHERE event_id=?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            return (
                BeanRef(str(row["run_id"]), int(row["sequence"])),
                str(payload.get("command_fingerprint", "")),
            )

    def event_history(self, bean_ref: BeanRef) -> tuple[BeanEvent, ...]:
        with self._lock:
            return tuple(
                event_from_dict(
                    {
                        "event_id": row["event_id"],
                        "kind": row["kind"],
                        "bean_ref": {
                            "run_id": bean_ref.run_id,
                            "sequence": bean_ref.sequence,
                        },
                        "timestamp_ns": row["timestamp_ns"],
                        "revision": row["revision"],
                        "stream_sequence": row["event_sequence"],
                        "payload": json.loads(row["payload_json"]),
                    }
                )
                for row in self._connection.execute(
                    """
                    SELECT * FROM registry_events WHERE run_id=? AND sequence=?
                    ORDER BY event_sequence
                    """,
                    (bean_ref.run_id, bean_ref.sequence),
                )
            )

    def events_since(
        self, after_sequence: int, *, limit: int = 1_000
    ) -> tuple[BeanEvent, ...]:
        if after_sequence < 0:
            raise ValueError("event cursor cannot be negative")
        if limit <= 0 or limit > 10_000:
            raise ValueError("event query limit must be between 1 and 10000")
        with self._lock:
            return tuple(
                event_from_dict(
                    {
                        "event_id": row["event_id"],
                        "kind": row["kind"],
                        "bean_ref": {
                            "run_id": row["run_id"],
                            "sequence": row["sequence"],
                        },
                        "timestamp_ns": row["timestamp_ns"],
                        "revision": row["revision"],
                        "stream_sequence": row["event_sequence"],
                        "payload": json.loads(row["payload_json"]),
                    }
                )
                for row in self._connection.execute(
                    """
                    SELECT * FROM registry_events WHERE event_sequence>?
                    ORDER BY event_sequence LIMIT ?
                    """,
                    (after_sequence, limit),
                )
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteBeanRepository:  # noqa: PYI034 - Python 3.10
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _save_track(
        self, record: BeanRecord, *, include_full_history: bool
    ) -> None:
        ref = record.bean_ref
        track = record.track
        self._connection.execute(
            """
            INSERT INTO track_states(
                run_id, sequence, registry_revision, status, timestamp_ns,
                x_mm, y_mm, vx_mm_s, vy_mm_s, covariance_json,
                hits, misses, last_bbox_px_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref.run_id,
                ref.sequence,
                record.revision,
                track.status.value,
                track.timestamp_ns,
                *track.state,
                _json([list(row) for row in track.covariance]),
                track.hits,
                track.misses,
                _json(list(track.last_bbox_px)),
            ),
        )
        observations = track.history if include_full_history else track.history[-1:]
        for observation in observations:
            detection = observation.detection
            self._connection.execute(
                """
                INSERT OR IGNORE INTO observations(
                    run_id, sequence, frame_index, timestamp_ns,
                    x_mm, y_mm, centroid_px_json, bbox_px_json,
                    area_px, solidity, mean_bgr_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref.run_id,
                    ref.sequence,
                    observation.frame_index,
                    observation.timestamp_ns,
                    *observation.position_mm,
                    _json(list(detection.centroid_px)),
                    _json(list(detection.bbox_px)),
                    detection.area_px,
                    detection.solidity,
                    _json(list(detection.mean_bgr)),
                ),
            )
        if record.prediction is not None:
            self._connection.execute(
                """
                INSERT INTO predictions(
                    run_id, sequence, registry_revision, timestamp_ns, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ref.run_id,
                    ref.sequence,
                    record.revision,
                    track.timestamp_ns,
                    _json(prediction_to_dict(record.prediction)),
                ),
            )

    def _load_track(self, bean_ref: BeanRef) -> TrackSnapshot:
        row = self._connection.execute(
            """
            SELECT * FROM track_states WHERE run_id=? AND sequence=?
            ORDER BY registry_revision DESC LIMIT 1
            """,
            (bean_ref.run_id, bean_ref.sequence),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"bean {bean_ref} has no persisted track state")
        history: list[Observation] = []
        for observation in self._connection.execute(
            """
            SELECT * FROM observations WHERE run_id=? AND sequence=?
            ORDER BY frame_index, timestamp_ns
            """,
            (bean_ref.run_id, bean_ref.sequence),
        ):
            centroid = json.loads(observation["centroid_px_json"])
            bbox = json.loads(observation["bbox_px_json"])
            mean_bgr = json.loads(observation["mean_bgr_json"])
            history.append(
                Observation(
                    frame_index=int(observation["frame_index"]),
                    timestamp_ns=int(observation["timestamp_ns"]),
                    detection=Detection(
                        centroid_px=(float(centroid[0]), float(centroid[1])),
                        bbox_px=tuple(int(value) for value in bbox),  # type: ignore[arg-type]
                        area_px=int(observation["area_px"]),
                        solidity=float(observation["solidity"]),
                        mean_bgr=tuple(float(value) for value in mean_bgr),  # type: ignore[arg-type]
                    ),
                    position_mm=(
                        float(observation["x_mm"]),
                        float(observation["y_mm"]),
                    ),
                )
            )
        covariance_value = json.loads(row["covariance_json"])
        bbox_value = json.loads(row["last_bbox_px_json"])
        return TrackSnapshot(
            bean_ref=bean_ref,
            status=TrackStatus(row["status"]),
            timestamp_ns=int(row["timestamp_ns"]),
            state=(
                float(row["x_mm"]),
                float(row["y_mm"]),
                float(row["vx_mm_s"]),
                float(row["vy_mm_s"]),
            ),
            covariance=tuple(
                tuple(float(value) for value in covariance_row)
                for covariance_row in covariance_value
            ),
            hits=int(row["hits"]),
            misses=int(row["misses"]),
            last_bbox_px=tuple(int(value) for value in bbox_value),  # type: ignore[arg-type]
            history=tuple(history),
        )

    def _create_schema(self) -> None:
        current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if current not in (0, SCHEMA_VERSION):
            raise RuntimeError(
                f"unsupported BeanRegistry database schema {current}; "
                f"expected {SCHEMA_VERSION}"
            )
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions(
                run_id TEXT PRIMARY KEY,
                created_timestamp_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS beans(
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_timestamp_ns INTEGER NOT NULL,
                updated_timestamp_ns INTEGER NOT NULL,
                PRIMARY KEY(run_id, sequence),
                FOREIGN KEY(run_id) REFERENCES sessions(run_id)
            );
            CREATE TABLE IF NOT EXISTS track_states(
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                registry_revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                x_mm REAL NOT NULL,
                y_mm REAL NOT NULL,
                vx_mm_s REAL NOT NULL,
                vy_mm_s REAL NOT NULL,
                covariance_json TEXT NOT NULL,
                hits INTEGER NOT NULL,
                misses INTEGER NOT NULL,
                last_bbox_px_json TEXT NOT NULL,
                PRIMARY KEY(run_id, sequence, registry_revision),
                FOREIGN KEY(run_id, sequence) REFERENCES beans(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS observations(
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                frame_index INTEGER NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                x_mm REAL NOT NULL,
                y_mm REAL NOT NULL,
                centroid_px_json TEXT NOT NULL,
                bbox_px_json TEXT NOT NULL,
                area_px INTEGER NOT NULL,
                solidity REAL NOT NULL,
                mean_bgr_json TEXT NOT NULL,
                PRIMARY KEY(run_id, sequence, frame_index),
                FOREIGN KEY(run_id, sequence) REFERENCES beans(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS predictions(
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                registry_revision INTEGER NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, sequence, registry_revision),
                FOREIGN KEY(run_id, sequence) REFERENCES beans(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS enrichments(
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                result_id TEXT NOT NULL UNIQUE,
                registry_revision INTEGER NOT NULL,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                value_json TEXT NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                model_version TEXT NOT NULL,
                confidence REAL,
                PRIMARY KEY(run_id, sequence, result_id),
                FOREIGN KEY(run_id, sequence) REFERENCES beans(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS sorting_decisions(
                decision_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                registry_revision INTEGER NOT NULL,
                source TEXT NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                actuation_timestamp_ns INTEGER NOT NULL,
                gate_indices_json TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                reason TEXT NOT NULL,
                acknowledged_timestamp_ns INTEGER,
                FOREIGN KEY(run_id, sequence) REFERENCES beans(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS registry_events(
                event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(run_id, sequence) REFERENCES beans(run_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS beans_status_index
                ON beans(run_id, status, sequence);
            CREATE INDEX IF NOT EXISTS registry_events_bean_index
                ON registry_events(run_id, sequence, event_sequence);
            """
        )
        self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._connection.commit()


def _json(value: object) -> str:
    return json.dumps(
        value, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
