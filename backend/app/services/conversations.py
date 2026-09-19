"""Conversation and query history.

An in-process store, deliberately. Persisting conversations would mean a
migration, a retention policy and a privacy story, none of which this project
needs to demonstrate — and a database-backed store would be the same code with
a different `dict`. What matters here is the *shape* of the state: bounded,
structured, and summarised rather than replayed.

Two bounds keep memory flat under sustained use:

* each conversation keeps at most `MAX_TURNS_RETAINED` turns,
* the store keeps at most `MAX_CONVERSATIONS`, evicting least-recently-used.

Without both, a long-running process grows without limit — the classic way an
in-memory cache becomes an outage.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.agents.runner import AgentRun
from app.agents.state import ConversationTurn

logger = logging.getLogger(__name__)

#: Turns retained per conversation. Older ones are dropped: a follow-up
#: realistically refers to the last few exchanges, not to one an hour ago.
MAX_TURNS_RETAINED = 20

#: Conversations retained process-wide, evicted least-recently-used.
MAX_CONVERSATIONS = 200

#: Stored query results, for `GET /api/query/{id}`.
MAX_STORED_QUERIES = 500


@dataclass(slots=True)
class Conversation:
    """One thread of questions and answers."""

    id: str
    created_at: dt.datetime
    updated_at: dt.datetime
    turns: list[ConversationTurn] = field(default_factory=list)

    @property
    def title(self) -> str:
        """A short label for the sidebar — the opening question."""
        if not self.turns:
            return "New conversation"
        first = self.turns[0].question
        return first[:60] + ("..." if len(first) > 60 else "")

    def summary(self) -> dict[str, Any]:
        return {
            "conversation_id": self.id,
            "title": self.title,
            "turn_count": len(self.turns),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class ConversationStore:
    """Thread-safe, bounded conversation and result store.

    Locked because FastAPI runs sync endpoints in a thread pool, so two requests
    genuinely can mutate the same dict concurrently.
    """

    def __init__(
        self,
        *,
        max_conversations: int = MAX_CONVERSATIONS,
        max_turns: int = MAX_TURNS_RETAINED,
        max_queries: int = MAX_STORED_QUERIES,
    ) -> None:
        self._conversations: OrderedDict[str, Conversation] = OrderedDict()
        self._queries: OrderedDict[str, AgentRun] = OrderedDict()
        self._max_conversations = max_conversations
        self._max_turns = max_turns
        self._max_queries = max_queries
        self._lock = threading.Lock()

    # -- conversations -----------------------------------------------------

    def create(self) -> Conversation:
        now = dt.datetime.now(dt.UTC)
        conversation = Conversation(id=uuid.uuid4().hex[:16], created_at=now, updated_at=now)
        with self._lock:
            self._conversations[conversation.id] = conversation
            self._evict_conversations()
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is not None:
                # Touch for LRU ordering.
                self._conversations.move_to_end(conversation_id)
            return conversation

    def get_or_create(self, conversation_id: str | None) -> Conversation:
        if conversation_id:
            existing = self.get(conversation_id)
            if existing is not None:
                return existing
        return self.create()

    def history_for(self, conversation_id: str | None) -> list[ConversationTurn]:
        """Turns available to the planner for follow-up resolution."""
        if not conversation_id:
            return []
        conversation = self.get(conversation_id)
        return list(conversation.turns) if conversation else []

    def record(self, conversation_id: str, run: AgentRun) -> None:
        """Append a completed run to its conversation and to the query store."""
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                conversation = Conversation(
                    id=conversation_id,
                    created_at=dt.datetime.now(dt.UTC),
                    updated_at=dt.datetime.now(dt.UTC),
                )
                self._conversations[conversation_id] = conversation

            # Only successful turns join the history. A failed turn resolved as
            # context would have a follow-up refining a query that never ran.
            if run.succeeded:
                conversation.turns.append(run.to_turn())
                if len(conversation.turns) > self._max_turns:
                    del conversation.turns[: -self._max_turns]

            conversation.updated_at = dt.datetime.now(dt.UTC)
            self._conversations.move_to_end(conversation_id)
            self._evict_conversations()

            self._queries[run.request_id] = run
            self._evict_queries()

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._conversations.values())
        items.sort(key=lambda c: c.updated_at, reverse=True)
        return [conversation.summary() for conversation in items[:limit]]

    # -- queries -----------------------------------------------------------

    def get_query(self, request_id: str) -> AgentRun | None:
        with self._lock:
            return self._queries.get(request_id)

    def list_queries(self, limit: int = 50, conversation_id: str = "") -> list[AgentRun]:
        with self._lock:
            runs = list(self._queries.values())
        if conversation_id:
            ids = {
                turn.question
                for conversation in [self.get(conversation_id)]
                if conversation
                for turn in conversation.turns
            }
            runs = [run for run in runs if run.question in ids]
        return list(reversed(runs))[:limit]

    def clear(self) -> None:
        """Drop everything. Used by tests to isolate state between cases."""
        with self._lock:
            self._conversations.clear()
            self._queries.clear()

    # -- internals ---------------------------------------------------------

    def _evict_conversations(self) -> None:
        while len(self._conversations) > self._max_conversations:
            evicted, _ = self._conversations.popitem(last=False)
            logger.debug("evicted conversation %s", evicted)

    def _evict_queries(self) -> None:
        while len(self._queries) > self._max_queries:
            self._queries.popitem(last=False)


#: Process-wide store. Injected into routes through a dependency so tests can
#: substitute a fresh instance.
_store = ConversationStore()


def get_store() -> ConversationStore:
    return _store
