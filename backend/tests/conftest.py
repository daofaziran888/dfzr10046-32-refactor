"""测试夹具：每个用例都从种子数据起步，共用进程级动作分派器。"""
from __future__ import annotations

import pytest

from app.services.dispatch import dispatcher
from app.store import store


@pytest.fixture(autouse=True)
def fresh_store():
    """每个用例前重置内存表与在途计划，避免跨用例串数据。"""
    store.reload()
    dispatcher().discard_all()
    yield
    dispatcher().discard_all()
    store.reload()
