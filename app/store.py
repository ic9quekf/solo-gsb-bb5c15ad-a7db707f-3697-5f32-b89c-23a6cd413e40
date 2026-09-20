"""In-memory policy storage with a reentrant lock.

The store keeps only *validated* policies.  Policies and their generated ids
live for the lifetime of the process; there is no persistence.
"""
from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from typing import Any, Optional


class PolicyStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._policies: dict[str, dict] = {}

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def put(self, policy_id: str, policy: dict) -> dict:
        with self._lock:
            record = {
                "id": policy_id,
                "rules": deepcopy(policy["rules"]),
                "rule_count": len(policy["rules"]),
                "priorities": sorted(
                    {r["priority"] for r in policy["rules"]}, reverse=True
                ),
            }
            self._policies[policy_id] = record
            return deepcopy(record)

    def get(self, policy_id: str) -> Optional[dict]:
        with self._lock:
            record = self._policies.get(policy_id)
            return deepcopy(record) if record else None

    def list(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "id": p["id"],
                    "rule_count": p["rule_count"],
                    "priorities": list(p["priorities"]),
                }
                for p in self._policies.values()
            ]

    def delete(self, policy_id: str) -> bool:
        with self._lock:
            return self._policies.pop(policy_id, None) is not None

    def count(self) -> int:
        with self._lock:
            return len(self._policies)

    def clear(self) -> None:
        """Test helper."""
        with self._lock:
            self._policies.clear()


store = PolicyStore()
