"""Forwarded request headers (SPEC.md §3.2).

The client sends every header it received, so this container holds an untrusted
bag of name/value pairs. Two properties are load-bearing and are the reason this
is not simply a `dict`:

* **Duplicates survive.** HTTP allows repeated headers, and a repeated identity
  header is either a broken gateway or a smuggling attempt. A dict silently
  keeps one of them, and no driver can then detect what happened.
* **Order survives.** Some gateway conventions are order-sensitive, and a driver
  that needs to reason about "the first one the proxy appended" can.

There is deliberately no `__getitem__`. Every lookup makes the caller say what
it wants when a header appears more than once — `sole()` to require exactly one,
`get_all()` to handle them itself. The dangerous option, "just give me one of
them", is not offered.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Sequence


class DuplicateHeaderError(Exception):
    """A header required to be unique appeared more than once.

    Per SPEC.md §3.2 a driver MUST turn this into `401`, never pick a winner.
    """

    def __init__(self, name: str, count: int) -> None:
        super().__init__(f"header {name!r} appears {count} times; expected at most one")
        self.name = name
        self.count = count


class Headers:
    """Ordered, duplicate-preserving, case-insensitive header collection."""

    __slots__ = ("_pairs",)

    def __init__(self, pairs: Iterable[tuple[str, str]] = ()) -> None:
        # Names are lowercased on the way in so driver matching is exact.
        # Clients are required to lowercase too; doing it again is cheap and
        # means a sloppy client cannot make a driver's comparison miss.
        self._pairs: list[tuple[str, str]] = [(n.lower(), v) for n, v in pairs]

    def __len__(self) -> int:
        return len(self._pairs)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._pairs)

    def __repr__(self) -> str:
        # Names only, never values: SPEC.md §1.4 forbids credential material in
        # diagnostics, and a repr ends up in logs and tracebacks by accident.
        return f"Headers({len(self._pairs)} headers: {sorted(self.names())})"

    def names(self) -> set[str]:
        return {n for n, _ in self._pairs}

    def get_all(self, name: str) -> list[str]:
        """Every value for `name`, in received order. Empty if absent."""
        wanted = name.lower()
        return [v for n, v in self._pairs if n == wanted]

    def count(self, name: str) -> int:
        return len(self.get_all(name))

    def sole(self, name: str) -> str | None:
        """The single value for `name`, or None if absent.

        Raises DuplicateHeaderError if it appears more than once — the caller
        must not be able to accidentally accept an ambiguous credential.
        """
        values = self.get_all(name)
        if not values:
            return None
        if len(values) > 1:
            raise DuplicateHeaderError(name.lower(), len(values))
        return values[0]

    def to_wire(self) -> list[list[str]]:
        return [[n, v] for n, v in self._pairs]

    @staticmethod
    def from_wire(raw: object) -> "Headers":
        """Parse and validate the `headers` field of a request.

        Raises ValueError with a message safe to surface (no header values —
        the whole point is that any of them may be a credential).
        """
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValueError("headers must be an array of [name, value] pairs")

        pairs: list[tuple[str, str]] = []
        for entry in raw:
            if (
                not isinstance(entry, Sequence)
                or isinstance(entry, (str, bytes))
                or len(entry) != 2
                or not all(isinstance(x, str) for x in entry)
            ):
                raise ValueError("each header entry must be a [name, value] string pair")
            name, value = entry
            if not name:
                raise ValueError("header name must not be empty")
            pairs.append((name.lower(), value))

        return Headers(pairs)
