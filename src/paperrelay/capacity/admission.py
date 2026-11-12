"""Pipeline-request admission: bounded FIFO queue in front of a concurrency limit.

A request either gets a slot immediately, waits in a FIFO queue of at most `max_queue`
waiters, or is rejected (`admission_queue_full`). A released slot is handed directly to
the oldest waiter, so new arrivals cannot overtake the queue. A waiter that is cancelled
(client disconnect, request deadline) is removed; if it had been handed a slot in the same
instant, the slot is passed on rather than leaked. `fail_queued` ends every waiter at once
(used when the daily provider budget is exhausted).

Asyncio-safe within one event loop; no cross-process coordination.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


class AdmissionRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Ticket:
    """Filled in by acquire(); readable by the caller even if the wait fails."""

    queued: bool = False
    queue_peak_observed: int = 0  # max queue depth (incl. this request) seen while waiting
    depth_on_arrival: int = 0


@dataclass
class _Waiter:
    request_id: str
    future: asyncio.Future[bool]
    ticket: Ticket


@dataclass
class AdmissionController:
    max_concurrency: int
    max_queue: int
    in_flight: int = 0
    peak_queue_depth: int = 0  # process-lifetime peak, for /capacity
    _waiters: deque[_Waiter] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.max_concurrency < 1 or self.max_queue < 0:
            raise ValueError("max_concurrency must be >= 1 and max_queue >= 0")

    @property
    def queue_depth(self) -> int:
        return len(self._waiters)

    async def acquire(
        self, request_id: str, ticket: Ticket | None = None,
        on_queued: Callable[[int, int], object] | None = None,
    ) -> Ticket:
        """Wait for a slot. Raises AdmissionRejected when the queue is full or failed;
        propagates CancelledError (deadline or disconnect) after removing the waiter."""
        ticket = ticket if ticket is not None else Ticket()
        if self.in_flight < self.max_concurrency and not self._waiters:
            self.in_flight += 1
            return ticket
        if len(self._waiters) >= self.max_queue:
            raise AdmissionRejected("admission_queue_full")
        waiter = _Waiter(request_id, asyncio.get_running_loop().create_future(), ticket)
        self._waiters.append(waiter)
        depth = len(self._waiters)
        ticket.queued, ticket.depth_on_arrival = True, depth
        self.peak_queue_depth = max(self.peak_queue_depth, depth)
        for w in self._waiters:  # every current waiter has now seen this depth
            w.ticket.queue_peak_observed = max(w.ticket.queue_peak_observed, depth)
        if on_queued is not None:
            on_queued(depth, self.in_flight)
        try:
            await waiter.future
        except asyncio.CancelledError:
            fut = waiter.future
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                self.release()  # slot was handed over just before cancellation: pass it on
            else:
                self._remove(waiter)
            raise
        return ticket

    def _remove(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass

    def release(self) -> None:
        """Free one slot: hand it to the oldest live waiter (in_flight unchanged) if any."""
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.future.done():
                waiter.future.set_result(True)
                return
        self.in_flight = max(0, self.in_flight - 1)

    def fail_queued(self, reason: str) -> int:
        """End every queued waiter with AdmissionRejected(reason). Returns how many."""
        failed = 0
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.future.done():
                waiter.future.set_exception(AdmissionRejected(reason))
                failed += 1
        return failed
