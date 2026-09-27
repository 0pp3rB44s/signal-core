from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from execution.state_store import JsonStateStore


def test_atomic_update_preserves_concurrent_writes(tmp_path):
    path = tmp_path / "shared_state.json"
    JsonStateStore(str(path)).save({"count": 0})

    def increment(_index: int) -> None:
        store = JsonStateStore(str(path))

        def apply(data: dict) -> dict:
            return {"count": int(data.get("count", 0)) + 1}

        store.update(default={"count": 0}, mutator=apply)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment, range(40)))

    assert JsonStateStore(str(path)).load(default={}) == {"count": 40}
