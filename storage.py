import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

TASK_COLUMNS: Tuple[str, ...] = (
    "issue_id",
    "title",
    "repository",
    "branch",
    "status",
    "session_id",
    "pr_url",
    "failure_category",
    "failure_reason",
    "created_at",
    "updated_at",
    "error",
    "session_url",
)


class TaskStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self._cursor() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    issue_id TEXT,
                    title TEXT,
                    repository TEXT,
                    branch TEXT,
                    status TEXT,
                    session_id TEXT,
                    pr_url TEXT,
                    failure_category TEXT,
                    failure_reason TEXT,
                    created_at REAL,
                    updated_at REAL,
                    error TEXT,
                    session_url TEXT
                )
                """
            )
            self._migrate_add_columns(connection)

    def _migrate_add_columns(self, connection: sqlite3.Connection) -> None:
        existing = {
            row[1]
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        migrations = [
            ("session_url", "TEXT"),
        ]
        for column_name, column_type in migrations:
            if column_name not in existing:
                connection.execute(
                    f"ALTER TABLE tasks ADD COLUMN {column_name} {column_type}"
                )

    def _row_to_task(self, row: sqlite3.Row) -> Dict[str, object]:
        return {column: row[column] for column in TASK_COLUMNS}

    def count(self) -> int:
        with self._cursor() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM tasks").fetchone()
        return int(row["total"])

    def load_tasks(self) -> Dict[str, dict]:
        with self._cursor() as connection:
            rows = connection.execute("SELECT * FROM tasks").fetchall()
        return {row["id"]: self._row_to_task(row) for row in rows}

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._cursor() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row is not None else None

    def insert_task(self, task_id: str, task: dict) -> None:
        columns = ", ".join(("id",) + TASK_COLUMNS)
        placeholders = ", ".join("?" for _ in range(len(TASK_COLUMNS) + 1))
        values = (task_id, *(task.get(column) for column in TASK_COLUMNS))
        with self._cursor() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO tasks ({columns}) VALUES ({placeholders})",
                values,
            )

    def update_task(self, task_id: str, **fields) -> Optional[dict]:
        updates = {key: value for key, value in fields.items() if key in TASK_COLUMNS}
        updates["updated_at"] = time.time()
        assignments = ", ".join(f"{column} = ?" for column in updates)
        with self._cursor() as connection:
            existing = connection.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if existing is None:
                return None
            connection.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",
                (*updates.values(), task_id),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row)

    def delete_task(self, task_id: str) -> bool:
        with self._cursor() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cursor.rowcount > 0

    def find_in_flight(
        self, repository: str, issue_id: str, statuses: Tuple[str, ...]
    ) -> Optional[str]:
        if not statuses:
            return None
        placeholders = ", ".join("?" for _ in statuses)
        with self._cursor() as connection:
            row = connection.execute(
                f"SELECT id FROM tasks WHERE repository = ? AND issue_id = ? "
                f"AND status IN ({placeholders}) LIMIT 1",
                (repository, str(issue_id), *statuses),
            ).fetchone()
        return row["id"] if row is not None else None

    def query_tasks(
        self,
        status: Optional[str] = None,
        repository: Optional[str] = None,
        time_from: Optional[float] = None,
        time_to: Optional[float] = None,
    ) -> List[Tuple[str, dict]]:
        clauses: List[str] = []
        params: List[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if repository:
            clauses.append("repository = ?")
            params.append(repository)
        if time_from is not None:
            clauses.append("created_at >= ?")
            params.append(time_from)
        if time_to is not None:
            clauses.append("created_at <= ?")
            params.append(time_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._cursor() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC", params
            ).fetchall()
        return [(row["id"], self._row_to_task(row)) for row in rows]

    def ping(self) -> bool:
        try:
            with self._cursor() as connection:
                connection.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def migrate_legacy_tasks(self, legacy_path: Path) -> int:
        if not legacy_path.exists() or self.count() > 0:
            return 0
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return 0
        if not isinstance(data, dict):
            return 0

        imported = 0
        columns = ", ".join(("id",) + TASK_COLUMNS)
        placeholders = ", ".join("?" for _ in range(len(TASK_COLUMNS) + 1))
        with self._cursor() as connection:
            for task_id, task in data.items():
                if not isinstance(task, dict):
                    continue
                values = (task_id, *(task.get(column) for column in TASK_COLUMNS))
                connection.execute(
                    f"INSERT OR REPLACE INTO tasks ({columns}) VALUES ({placeholders})",
                    values,
                )
                imported += 1

        if imported:
            try:
                legacy_path.rename(legacy_path.with_name(legacy_path.name + ".imported"))
            except OSError:
                pass
        return imported
