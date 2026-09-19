"""Engine/session management and the guarded read-only query executor.

All agent-generated SQL flows through `execute_readonly`, which applies the
statement timeout, the row cap, and the read-only connection role. Nothing
else in the application opens a database connection for user-supplied SQL.
"""

from app.database.executor import QueryResult, execute_readonly
from app.database.session import (
    check_database_health,
    dispose_engines,
    get_admin_engine,
    get_readonly_engine,
    readonly_session,
)

__all__ = [
    "QueryResult",
    "check_database_health",
    "dispose_engines",
    "execute_readonly",
    "get_admin_engine",
    "get_readonly_engine",
    "readonly_session",
]
