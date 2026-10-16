"""A stand-in for requests.Session that serves canned responses per (method, path)."""
from collections import deque


class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text if text else ("" if payload is None else "json")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, routes: dict):
        # routes: {(method, path): [FakeResponse, ...]} served in order; last one repeats.
        self.routes = {k: deque(v) for k, v in routes.items()}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.headers: dict = {}

    def request(self, method, url, json=None, timeout=None):
        path = url.split("/ecari-dispoweb", 1)[1]
        self.calls.append((method, path, json))
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected call {method} {path}")
        resp = queue[0] if len(queue) == 1 else queue.popleft()
        return resp

    def paths(self):
        return [(m, p) for m, p, _ in self.calls]
