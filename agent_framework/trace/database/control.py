"""注入式 DB-API 适配器与完整 Trace schema。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping


Dialect = Literal["sqlite", "mysql"]
ConnectionFactory = Callable[[], Any]


class TraceDatabaseError(RuntimeError):
    """Raised when the injected database cannot initialize the Trace schema."""


class TraceDatabase:
    """四层 Writer 共用的轻量 DB-API 方言适配器。

    Configuration and connection pooling stay in the application. Trace only
    receives a connection factory and the SQL dialect it should emit.
    """

    def __init__(
        self,
        connect_factory: ConnectionFactory,
        *,
        dialect: Dialect,
    ) -> None:
        if dialect not in {"sqlite", "mysql"}:
            raise ValueError("dialect must be 'sqlite' or 'mysql'")
        self._connect_factory = connect_factory
        self.dialect = dialect

    def connect(self):
        connection = self._connect_factory()
        if connection is None:
            raise TraceDatabaseError("connect_factory returned None")
        if self.dialect == "sqlite":
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()
        return connection

    def execute(
        self,
        cursor: Any,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> Any:
        statement = sql.replace("%s", "?") if self.dialect == "sqlite" else sql
        return cursor.execute(statement, parameters)

    def begin(self, connection: Any, cursor: Any) -> None:
        if self.dialect == "sqlite":
            cursor.execute("BEGIN IMMEDIATE")
            return
        start_transaction = getattr(connection, "start_transaction", None)
        if callable(start_transaction):
            start_transaction()
        else:
            cursor.execute("START TRANSACTION")

    @staticmethod
    def commit(connection: Any, cursor: Any) -> None:
        commit = getattr(connection, "commit", None)
        if callable(commit):
            commit()
        else:
            cursor.execute("COMMIT")

    @staticmethod
    def rollback(connection: Any, cursor: Any) -> None:
        rollback = getattr(connection, "rollback", None)
        if callable(rollback):
            rollback()
        else:
            cursor.execute("ROLLBACK")

    def timestamp(self, value: datetime | None = None) -> Any:
        current = self.utc_datetime(value)
        naive_utc = current.replace(tzinfo=None)
        if self.dialect == "sqlite":
            return naive_utc.isoformat(sep=" ", timespec="milliseconds")
        return naive_utc

    @staticmethod
    def utc_datetime(value: datetime | str | None = None) -> datetime:
        current: datetime
        if isinstance(value, str):
            current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            current = value or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    @staticmethod
    def row_to_dict(cursor: Any, row: Any) -> dict[str, Any]:
        if isinstance(row, Mapping):
            result = dict(row)
        else:
            columns = [column[0] for column in cursor.description]
            result = dict(zip(columns, row))
        for key, value in list(result.items()):
            if isinstance(value, datetime):
                normalized = (
                    value.replace(tzinfo=timezone.utc)
                    if value.tzinfo is None or value.utcoffset() is None
                    else value.astimezone(timezone.utc)
                )
                result[key] = normalized.isoformat(timespec="milliseconds")
        return result

    def ensure_schema(self) -> None:
        connection = self.connect()
        cursor = connection.cursor()
        try:
            for statement in self._schema():
                self.execute(cursor, statement)
            self._ensure_compatibility_columns(cursor)
            self.commit(connection, cursor)
        except Exception as exc:
            try:
                self.rollback(connection, cursor)
            except Exception:
                pass
            raise TraceDatabaseError(
                f"cannot initialize trace tables: {type(exc).__name__}"
            ) from exc
        finally:
            cursor.close()
            connection.close()

    def _ensure_compatibility_columns(self, cursor: Any) -> None:
        """Add columns introduced after the original eight-table schema."""

        if self.dialect == "sqlite":
            compatibility = {
                "llm_call_info": {
                    "cached_tokens": "INTEGER NOT NULL DEFAULT 0",
                    "reasoning_tokens": "INTEGER NOT NULL DEFAULT 0",
                },
                "stage_run_info": {
                    "upstream_json": "JSON NOT NULL DEFAULT '{}'",
                    "outputs_json": "JSON NOT NULL DEFAULT '{}'",
                    "dag_json": "JSON NOT NULL DEFAULT '{}'",
                },
            }
        else:
            compatibility = {
                "llm_call_info": {
                    "cached_tokens": "BIGINT UNSIGNED NOT NULL DEFAULT 0",
                    "reasoning_tokens": "BIGINT UNSIGNED NOT NULL DEFAULT 0",
                },
                # Existing rows predate this field, so migration keeps it
                # nullable. Every new StageStarted insert supplies valid JSON.
                "stage_run_info": {
                    "upstream_json": "JSON NULL",
                    "outputs_json": "JSON NULL",
                    "dag_json": "JSON NULL",
                },
            }

        for table, definitions in compatibility.items():
            columns = self._columns(cursor, table)
            for name, definition in definitions.items():
                if name not in columns:
                    self.execute(
                        cursor,
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}",
                    )

    def _columns(self, cursor: Any, table: str) -> set[str]:
        """Return columns for an internal schema table name."""

        if self.dialect == "sqlite":
            self.execute(cursor, f"PRAGMA table_info({table})")
            return {
                str(row["name"] if isinstance(row, Mapping) else row[1])
                for row in cursor.fetchall()
            }
        self.execute(cursor, f"SHOW COLUMNS FROM {table}")
        return {
            str(row["Field"] if isinstance(row, Mapping) else row[0])
            for row in cursor.fetchall()
        }

    def _schema(self) -> tuple[str, ...]:
        return (
            self._sqlite_schema()
            if self.dialect == "sqlite"
            else self._mysql_schema()
        )

    @staticmethod
    def _sqlite_schema() -> tuple[str, ...]:
        return (
            """
            CREATE TABLE IF NOT EXISTS llm_call_info (
                llm_call_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                flow_id TEXT,
                stage_id TEXT,
                agent_id TEXT,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                request_json JSON NOT NULL
                    CHECK (json_valid(request_json)),
                total_input_tokens INTEGER NOT NULL DEFAULT 0
                    CHECK (total_input_tokens >= 0),
                total_output_tokens INTEGER NOT NULL DEFAULT 0
                    CHECK (total_output_tokens >= 0),
                total_tokens INTEGER NOT NULL DEFAULT 0
                    CHECK (total_tokens >= 0),
                cached_tokens INTEGER NOT NULL DEFAULT 0
                    CHECK (cached_tokens >= 0),
                reasoning_tokens INTEGER NOT NULL DEFAULT 0
                    CHECK (reasoning_tokens >= 0),
                retry_count INTEGER NOT NULL DEFAULT 0
                    CHECK (retry_count >= 0),
                duration_ms INTEGER
                    CHECK (duration_ms IS NULL OR duration_ms >= 0),
                usage_complete INTEGER NOT NULL DEFAULT 1
                    CHECK (usage_complete IN (0, 1)),
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS llm_run_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                llm_call_id TEXT NOT NULL,
                seq INTEGER NOT NULL CHECK (seq >= 1),
                event_type TEXT NOT NULL,
                attempt_no INTEGER
                    CHECK (attempt_no IS NULL OR attempt_no >= 1),
                level TEXT NOT NULL DEFAULT 'INFO',
                duration_ms INTEGER
                    CHECK (duration_ms IS NULL OR duration_ms >= 0),
                payload_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(payload_json)),
                recorded_at TEXT NOT NULL,
                UNIQUE (llm_call_id, seq),
                FOREIGN KEY (llm_call_id)
                    REFERENCES llm_call_info(llm_call_id)
                    ON DELETE RESTRICT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_llm_info_trace "
            "ON llm_call_info(trace_id)",
            "CREATE INDEX IF NOT EXISTS idx_llm_info_flow "
            "ON llm_call_info(flow_id)",
            "CREATE INDEX IF NOT EXISTS idx_llm_info_stage "
            "ON llm_call_info(stage_id)",
            "CREATE INDEX IF NOT EXISTS idx_llm_info_agent "
            "ON llm_call_info(agent_id)",
            "CREATE INDEX IF NOT EXISTS idx_llm_info_created "
            "ON llm_call_info(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_llm_record_call "
            "ON llm_run_records(llm_call_id)",
            "CREATE INDEX IF NOT EXISTS idx_llm_record_type "
            "ON llm_run_records(event_type)",
            "CREATE INDEX IF NOT EXISTS idx_llm_record_time "
            "ON llm_run_records(recorded_at)",
            """
            CREATE TABLE IF NOT EXISTS agent_run_info (
                agent_run_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                flow_id TEXT,
                stage_id TEXT,
                session_id TEXT,
                agent TEXT NOT NULL,
                task_type TEXT,
                turn_no INTEGER NOT NULL DEFAULT 1 CHECK (turn_no >= 1),
                status TEXT,
                inputs_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(inputs_json)),
                tool_iterations INTEGER NOT NULL DEFAULT 0
                    CHECK (tool_iterations >= 0),
                output_retries INTEGER NOT NULL DEFAULT 0
                    CHECK (output_retries >= 0),
                llm_calls INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),
                tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls >= 0),
                tool_failures INTEGER NOT NULL DEFAULT 0
                    CHECK (tool_failures >= 0),
                total_input_tokens INTEGER NOT NULL DEFAULT 0,
                total_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                repairs_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(repairs_json)),
                output_keys_json JSON NOT NULL DEFAULT '[]'
                    CHECK (json_valid(output_keys_json)),
                duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
                error TEXT,
                created_at TEXT NOT NULL,
                ended_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_run_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                agent_run_id TEXT NOT NULL,
                seq INTEGER NOT NULL CHECK (seq >= 1),
                event_type TEXT NOT NULL,
                step_index INTEGER
                    CHECK (step_index IS NULL OR step_index >= 1),
                level TEXT NOT NULL DEFAULT 'INFO',
                payload_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(payload_json)),
                recorded_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_agent_run_trace ON agent_run_info (trace_id)",
            "CREATE INDEX IF NOT EXISTS idx_agent_run_flow ON agent_run_info (flow_id)",
            "CREATE INDEX IF NOT EXISTS idx_agent_records_run ON agent_run_records (agent_run_id, seq)",
            """
            CREATE TABLE IF NOT EXISTS agent_session_info (
                session_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                flow_id TEXT,
                stage_id TEXT,
                agent TEXT NOT NULL,
                task_type TEXT,
                turns INTEGER NOT NULL DEFAULT 0 CHECK (turns >= 0),
                llm_calls INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),
                tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls >= 0),
                tool_failures INTEGER NOT NULL DEFAULT 0
                    CHECK (tool_failures >= 0),
                output_retries INTEGER NOT NULL DEFAULT 0
                    CHECK (output_retries >= 0),
                total_tokens INTEGER NOT NULL DEFAULT 0
                    CHECK (total_tokens >= 0),
                last_status TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_agent_session_trace ON agent_session_info (trace_id)",
            """
            CREATE TABLE IF NOT EXISTS workflow_run_info (
                workflow_run_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT,
                stages_total INTEGER NOT NULL DEFAULT 0,
                stages_done INTEGER NOT NULL DEFAULT 0,
                failed_stage TEXT,
                failed_stages_json JSON NOT NULL DEFAULT '[]'
                    CHECK (json_valid(failed_stages_json)),
                inputs_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(inputs_json)),
                stage_names_json JSON NOT NULL DEFAULT '[]'
                    CHECK (json_valid(stage_names_json)),
                total_input_tokens INTEGER NOT NULL DEFAULT 0,
                total_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER,
                error TEXT,
                created_at TEXT NOT NULL,
                ended_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS stage_run_info (
                stage_run_id TEXT PRIMARY KEY,
                workflow_run_id TEXT,
                trace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                seq INTEGER NOT NULL DEFAULT 1,
                status TEXT,
                steps INTEGER NOT NULL DEFAULT 0,
                skipped_steps INTEGER NOT NULL DEFAULT 0,
                failed_steps INTEGER NOT NULL DEFAULT 0,
                agent_runs INTEGER NOT NULL DEFAULT 0,
                inputs_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(inputs_json)),
                upstream_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(upstream_json)),
                outputs_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(outputs_json)),
                dag_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(dag_json)),
                output_keys_json JSON NOT NULL DEFAULT '[]'
                    CHECK (json_valid(output_keys_json)),
                total_input_tokens INTEGER NOT NULL DEFAULT 0,
                total_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER,
                error TEXT,
                created_at TEXT NOT NULL,
                ended_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS stage_step_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                step_id TEXT NOT NULL UNIQUE,
                stage_run_id TEXT NOT NULL,
                seq INTEGER NOT NULL CHECK (seq >= 1),
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'agent',
                status TEXT,
                skipped INTEGER NOT NULL DEFAULT 0 CHECK (skipped IN (0,1)),
                skip_reason TEXT,
                agent_profile_json JSON NOT NULL DEFAULT '{}'
                    CHECK (json_valid(agent_profile_json)),
                agent_runs_json JSON NOT NULL DEFAULT '[]'
                    CHECK (json_valid(agent_runs_json)),
                output_keys_json JSON NOT NULL DEFAULT '[]'
                    CHECK (json_valid(output_keys_json)),
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER,
                error TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_wf_trace ON workflow_run_info (trace_id)",
            "CREATE INDEX IF NOT EXISTS idx_stage_wf ON stage_run_info (workflow_run_id, seq)",
            "CREATE INDEX IF NOT EXISTS idx_step_stage ON stage_step_records (stage_run_id, seq)",
        )

    @staticmethod
    def _mysql_schema() -> tuple[str, ...]:
        return (
            """
            CREATE TABLE IF NOT EXISTS llm_call_info (
                llm_call_id VARCHAR(64) PRIMARY KEY,
                trace_id VARCHAR(64) NOT NULL,
                flow_id VARCHAR(64),
                stage_id VARCHAR(64),
                agent_id VARCHAR(64),
                provider VARCHAR(64) NOT NULL,
                model VARCHAR(191) NOT NULL,
                request_json JSON NOT NULL,
                total_input_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                total_output_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                total_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                cached_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                reasoning_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
                retry_count INT UNSIGNED NOT NULL DEFAULT 0,
                duration_ms BIGINT UNSIGNED,
                usage_complete BOOLEAN NOT NULL DEFAULT TRUE,
                created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                INDEX idx_trace_id (trace_id),
                INDEX idx_flow_id (flow_id),
                INDEX idx_stage_id (stage_id),
                INDEX idx_agent_id (agent_id),
                INDEX idx_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS llm_run_records (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                event_id VARCHAR(64) NOT NULL,
                llm_call_id VARCHAR(64) NOT NULL,
                seq INT UNSIGNED NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                attempt_no INT UNSIGNED,
                level VARCHAR(20) NOT NULL DEFAULT 'INFO',
                duration_ms BIGINT UNSIGNED,
                payload_json JSON NOT NULL,
                recorded_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                UNIQUE KEY uq_llm_record_event (event_id),
                UNIQUE KEY uq_llm_record_seq (llm_call_id, seq),
                INDEX idx_llm_record_call (llm_call_id),
                INDEX idx_llm_record_type (event_type),
                INDEX idx_llm_record_time (recorded_at),
                CONSTRAINT fk_llm_record_call
                    FOREIGN KEY (llm_call_id)
                    REFERENCES llm_call_info(llm_call_id)
                    ON DELETE RESTRICT
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_run_info (
                agent_run_id VARCHAR(64) PRIMARY KEY,
                trace_id VARCHAR(64) NOT NULL,
                flow_id VARCHAR(64),
                stage_id VARCHAR(64),
                session_id VARCHAR(64),
                agent VARCHAR(128) NOT NULL,
                task_type VARCHAR(128),
                turn_no INT NOT NULL DEFAULT 1,
                status VARCHAR(32),
                inputs_json JSON NOT NULL,
                tool_iterations INT NOT NULL DEFAULT 0,
                output_retries INT NOT NULL DEFAULT 0,
                llm_calls INT NOT NULL DEFAULT 0,
                tool_calls INT NOT NULL DEFAULT 0,
                tool_failures INT NOT NULL DEFAULT 0,
                total_input_tokens INT NOT NULL DEFAULT 0,
                total_output_tokens INT NOT NULL DEFAULT 0,
                total_tokens INT NOT NULL DEFAULT 0,
                cached_tokens INT NOT NULL DEFAULT 0,
                reasoning_tokens INT NOT NULL DEFAULT 0,
                repairs_json JSON NOT NULL,
                output_keys_json JSON NOT NULL,
                duration_ms INT,
                error TEXT,
                created_at DATETIME(3) NOT NULL,
                ended_at DATETIME(3),
                INDEX idx_agent_run_trace (trace_id),
                INDEX idx_agent_run_flow (flow_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_run_records (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                event_id VARCHAR(96) NOT NULL UNIQUE,
                agent_run_id VARCHAR(64) NOT NULL,
                seq INT NOT NULL,
                event_type VARCHAR(32) NOT NULL,
                step_index INT,
                level VARCHAR(16) NOT NULL DEFAULT 'INFO',
                payload_json JSON NOT NULL,
                recorded_at DATETIME(3) NOT NULL,
                INDEX idx_agent_records_run (agent_run_id, seq)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_session_info (
                session_id VARCHAR(64) PRIMARY KEY,
                trace_id VARCHAR(64) NOT NULL,
                flow_id VARCHAR(64),
                stage_id VARCHAR(64),
                agent VARCHAR(128) NOT NULL,
                task_type VARCHAR(128),
                turns INT NOT NULL DEFAULT 0,
                llm_calls INT NOT NULL DEFAULT 0,
                tool_calls INT NOT NULL DEFAULT 0,
                tool_failures INT NOT NULL DEFAULT 0,
                output_retries INT NOT NULL DEFAULT 0,
                total_tokens INT NOT NULL DEFAULT 0,
                cached_tokens INT NOT NULL DEFAULT 0,
                reasoning_tokens INT NOT NULL DEFAULT 0,
                last_status VARCHAR(32),
                created_at DATETIME(3) NOT NULL,
                updated_at DATETIME(3) NOT NULL,
                INDEX idx_agent_session_trace (trace_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS workflow_run_info (
                workflow_run_id VARCHAR(64) PRIMARY KEY,
                trace_id VARCHAR(64) NOT NULL,
                name VARCHAR(128) NOT NULL,
                status VARCHAR(32),
                stages_total INT NOT NULL DEFAULT 0,
                stages_done INT NOT NULL DEFAULT 0,
                failed_stage VARCHAR(128),
                failed_stages_json JSON NOT NULL,
                inputs_json JSON NOT NULL,
                stage_names_json JSON NOT NULL,
                total_input_tokens INT NOT NULL DEFAULT 0,
                total_output_tokens INT NOT NULL DEFAULT 0,
                total_tokens INT NOT NULL DEFAULT 0,
                cached_tokens INT NOT NULL DEFAULT 0,
                reasoning_tokens INT NOT NULL DEFAULT 0,
                duration_ms INT,
                error TEXT,
                created_at DATETIME(3) NOT NULL,
                ended_at DATETIME(3),
                INDEX idx_wf_trace (trace_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS stage_run_info (
                stage_run_id VARCHAR(64) PRIMARY KEY,
                workflow_run_id VARCHAR(64),
                trace_id VARCHAR(64) NOT NULL,
                name VARCHAR(128) NOT NULL,
                seq INT NOT NULL DEFAULT 1,
                status VARCHAR(32),
                steps INT NOT NULL DEFAULT 0,
                skipped_steps INT NOT NULL DEFAULT 0,
                failed_steps INT NOT NULL DEFAULT 0,
                agent_runs INT NOT NULL DEFAULT 0,
                inputs_json JSON NOT NULL,
                upstream_json JSON NOT NULL,
                outputs_json JSON NOT NULL,
                dag_json JSON NOT NULL,
                output_keys_json JSON NOT NULL,
                total_input_tokens INT NOT NULL DEFAULT 0,
                total_output_tokens INT NOT NULL DEFAULT 0,
                total_tokens INT NOT NULL DEFAULT 0,
                cached_tokens INT NOT NULL DEFAULT 0,
                reasoning_tokens INT NOT NULL DEFAULT 0,
                duration_ms INT,
                error TEXT,
                created_at DATETIME(3) NOT NULL,
                ended_at DATETIME(3),
                INDEX idx_stage_wf (workflow_run_id, seq)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS stage_step_records (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                step_id VARCHAR(64) NOT NULL UNIQUE,
                stage_run_id VARCHAR(64) NOT NULL,
                seq INT NOT NULL,
                name VARCHAR(255) NOT NULL,
                kind VARCHAR(32) NOT NULL DEFAULT 'agent',
                status VARCHAR(32),
                skipped TINYINT NOT NULL DEFAULT 0,
                skip_reason TEXT,
                agent_profile_json JSON NOT NULL,
                agent_runs_json JSON NOT NULL,
                output_keys_json JSON NOT NULL,
                total_tokens INT NOT NULL DEFAULT 0,
                cached_tokens INT NOT NULL DEFAULT 0,
                reasoning_tokens INT NOT NULL DEFAULT 0,
                duration_ms INT,
                error TEXT,
                started_at DATETIME(3) NOT NULL,
                ended_at DATETIME(3),
                INDEX idx_step_stage (stage_run_id, seq)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )


__all__ = [
    "ConnectionFactory",
    "Dialect",
    "TraceDatabase",
    "TraceDatabaseError",
]
