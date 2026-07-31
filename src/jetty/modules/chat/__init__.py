"""The `chat` module — spec/chat-v1.md.

A Google Chat REST API v1 subset served as a foreign protocol under `/chat`
on the control listener (SPEC.md §2.1): stock clients of that API reach an
internal chat service by changing only their base URL.

Layout:

- ``driver``  — the internal representation and the ``ChatDriver`` protocol
- ``mock``    — in-memory driver seeded from configuration
- ``module``  — the emulated surface: URL layout, translation, error shape
"""
