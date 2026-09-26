"""The ``FunctionDriver`` protocol and its typed failures (functions-v1 §6).

The surface (module.py) validates the wire contract — the name syntax — and
dispatches to a driver; a driver owns the set of functions it provides and
their execution. Nothing in this file knows about URLs or HTTP, and nothing
anywhere in this module knows what a payload means: the bytes belong to the
application that defined the function.

A driver is built by a **factory** taking the whole ``[modules.functions]``
table, so it validates its own keys the way a module validates its own
(``jetty.config``). Factories are found by name (``register_driver``) or by
a ``package.module:attribute`` path in config (functions-v1 §3), which is
how a library-backed driver reaches the reference binary without a fork.

Lifecycle: the module awaits ``startup()`` at boot and ``shutdown()`` at
exit when the driver defines them (both optional). A ``startup`` that
raises aborts boot — a driver whose backend it cannot open must refuse to
serve, not serve errors forever (SPEC.md §1.2).

``call`` is a coroutine; a driver that does blocking I/O keeps it off the
event loop (``starlette.concurrency.run_in_threadpool`` or its own pool).

Error contract (functions-v1 §6): a driver raises the typed exceptions
below for the two conditions the protocol distinguishes. Anything else it
raises is a backend that could not be reached: the surface maps it to
``503 upstream_unavailable``, never to a fabricated success (SPEC.md §1.2).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol


class UnknownFunction(Exception):
    """No function of that name is provided → ``404 unknown_function``."""


class FunctionFailed(Exception):
    """The function ran and produced no response → ``502 upstream_error``."""


class FunctionDriver(Protocol):
    def functions(self) -> list[str]: ...
    async def call(self, name: str, payload: bytes) -> bytes: ...


#: Builds a driver from the module's settings table. Raising here aborts
#: boot, which is where a bad driver configuration belongs.
DriverFactory = Callable[[Mapping[str, Any]], FunctionDriver]
