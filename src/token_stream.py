from collections import deque
from threading import Lock

_TOKEN_QUEUES = {}
_LOCK = Lock()


def push_token(thread_id, role, chunk):
    if chunk is None:
        return
    with _LOCK:
        queue = _TOKEN_QUEUES.setdefault(thread_id, deque())
        queue.append({"role": role, "chunk": chunk})


def pop_tokens(thread_id):
    with _LOCK:
        queue = _TOKEN_QUEUES.get(thread_id)
        if not queue:
            return []
        items = list(queue)
        queue.clear()
        return items


def clear_tokens(thread_id):
    with _LOCK:
        _TOKEN_QUEUES.pop(thread_id, None)
