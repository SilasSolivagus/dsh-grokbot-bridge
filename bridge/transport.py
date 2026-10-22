"""Transport interface for the M1.5 task bus.

The orchestrator owns envelope validation and DSH state; a transport only
carries an already validated task request to the other side and carries raw
reply envelopes back. Concrete transports (a GitHub issue thread, an HTTP
relay, or a test fake) implement :class:`Transport`; the orchestrator depends
on this interface rather than on any adapter module.

Contract:

* ``open(request)`` creates one logical thread for a validated task request
  and returns an opaque, non-empty reference.
* ``replies(ref, after=None)`` returns the reply envelopes received after
  ``after`` and the cursor to pass on the next call. ``after=None`` starts at
  the beginning of the thread. The cursor is opaque to the caller; ``None``
  means no newer cursor is available (for example, an empty poll).
* ``close(ref)`` releases the thread.

Transport adapters raise :class:`TransportError` when the backing service
fails. This module performs no I/O and imports no adapter or orchestrator
module.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["Transport", "TransportError"]


class TransportError(Exception):
    """Raised when a transport adapter cannot complete an operation."""


class Transport(ABC):
    """Abstract wire boundary for task requests and reply envelopes."""

    @abstractmethod
    def open(self, request: Dict[str, Any]) -> str:
        """Create a thread for ``request`` and return its reference."""
        raise NotImplementedError("Transport.open")

    @abstractmethod
    def replies(
        self,
        ref: str,
        after: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return replies after ``after`` and the next cursor."""
        raise NotImplementedError("Transport.replies")

    @abstractmethod
    def close(self, ref: str) -> None:
        """Release the thread identified by ``ref``."""
        raise NotImplementedError("Transport.close")
