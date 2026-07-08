#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Redis checkpointer factory for the agentic-RAG LangGraph.

LangGraph persists the graph state at every node boundary through a
*checkpointer*. We back it with the same Redis instance RAGFlow already
runs (``rag.utils.redis_conn``), so a mid-run failure can resume the
turn from the last committed node instead of re-executing the whole
procedure.

The checkpointer is created per run as an async context manager — the
LangGraph Redis saver needs to open its own async redis client and run
a one-time ``setup()`` that creates the checkpoint indices. We derive
the connection URL from RAGFlow's existing redis config so no extra
configuration is introduced.

``langgraph-checkpoint-redis`` and Redis Search support are optional:
importing this module never imports the package at module load, and a
plain Redis server falls back to no checkpoint persistence.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


def _is_redis_search_unavailable(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "unknown command" in message
        and (
            "ft._list" in message
            or "ft.create" in message
            or "ft.info" in message
        )
    )


def _redis_url_from_config() -> str:
    """Build a ``redis://`` URL from RAGFlow's base redis config.

    Mirrors the parsing in :class:`rag.utils.redis_conn.RedisDB.__open__`:
    ``host`` is stored as ``"<host>:<port>"``, ``db`` defaults to 1, and
    optional ``username`` / ``password`` are applied when present.
    """
    from common import settings

    try:
        cfg = settings.decrypt_database_config(name="redis")
    except Exception:
        cfg = settings.get_base_config("redis", {}) or {}

    host_port = str(cfg.get("host", "localhost:6379"))
    host = host_port.split(":")[0] or "localhost"
    try:
        port = int(host_port.split(":")[1])
    except (IndexError, ValueError):
        port = 6379
    db = int(cfg.get("db", 1) or 1)

    username = cfg.get("username") or ""
    password = cfg.get("password") or ""
    if username or password:
        auth = f"{username}:{password}@"
    else:
        auth = ""
    return f"redis://{auth}{host}:{port}/{db}"


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[Any]:
    """Yield an initialised LangGraph async Redis checkpointer.

    Usage::

        async with open_checkpointer() as cp:
            graph = build_graph(tools, checkpointer=cp)
            async for update in graph.astream(state, config):
                ...

    The ``AsyncRedisSaver.from_conn_string`` context manager owns the
    redis client lifecycle and runs setup. Import is local so a missing
    ``langgraph-checkpoint-redis`` package, or a plain Redis server
    without Redis Search commands, only disables checkpoint persistence.
    """
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    except ModuleNotFoundError:
        logging.warning(
            "agentic_rag: langgraph-checkpoint-redis is not installed; "
            "running without checkpoint persistence"
        )
        yield None
        return

    url = _redis_url_from_config()
    try:
        async with AsyncRedisSaver.from_conn_string(url) as saver:
            yield saver
    except Exception as exc:
        if _is_redis_search_unavailable(exc):
            logging.warning(
                "agentic_rag: Redis checkpointer requires Redis Search "
                "commands (FT.*), but the configured Redis server does not "
                "provide them; running without checkpoint persistence"
            )
        else:
            logging.exception(
                "agentic_rag: Redis checkpointer unavailable; running without "
                "checkpoint persistence"
            )
        yield None
