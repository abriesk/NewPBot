"""Connector contracts (IMPLEMENTATION.md §9).

Adding a channel means: one `Transport` implementation, one emitter in
app/render/markdown.py, one inbound router if the channel is interactive, and a
`Channel` enum value. It MUST NOT require touching anything under
app/core/services/ -- that is the property this interface exists to protect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.core.enums import Channel

#: §9. Telegram rejects callback data over 64 bytes, so the payload is
#: `<action>:<request_id>` and the handler looks up the rest.
CALLBACK_DATA_LIMIT = 64


@dataclass(frozen=True, slots=True)
class Action:
    """One thing the recipient can do about a message.

    The core emits the *semantic* action; each transport decides how to express
    it. Telegram renders an inline button, email a signed link, the web a form
    button (DESIGN.md §3.2).
    """

    key: str  # 'accept' | 'counter' | 'decline' | 'approve' | 'propose' | ...
    label: str  # already localised
    url: str | None = None  # email and web
    callback_data: str | None = None  # telegram, <= 64 bytes

    def __post_init__(self) -> None:
        if self.callback_data is not None:
            encoded = len(self.callback_data.encode())
            if encoded > CALLBACK_DATA_LIMIT:
                raise ValueError(
                    f"callback_data is {encoded} bytes, over Telegram's "
                    f"{CALLBACK_DATA_LIMIT}-byte limit: {self.callback_data!r}"
                )


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """What a transport is handed.

    `parts` is a list because Telegram splits at block boundaries (§11.2); every
    other channel gets one part and joins it.
    """

    parts: list[str]
    subject: str | None = None  # email only
    actions: list[Action] = field(default_factory=list)
    parse_mode: str | None = None

    @property
    def text(self) -> str:
        """The whole message as one string, for channels that do not split."""
        return "\n\n".join(self.parts)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The outcome of one send attempt.

    `permanent_failure` is the important field: a blocked bot or an invalid
    address must not be retried six times with backoff (§14), and must not be
    confused with a timeout.
    """

    ok: bool
    permanent_failure: bool = False
    error: str | None = None

    @classmethod
    def success(cls) -> DeliveryResult:
        return cls(ok=True)

    @classmethod
    def transient(cls, error: str) -> DeliveryResult:
        return cls(ok=False, permanent_failure=False, error=error)

    @classmethod
    def permanent(cls, error: str) -> DeliveryResult:
        return cls(ok=False, permanent_failure=True, error=error)


@runtime_checkable
class Transport(Protocol):
    """One outbound channel."""

    channel: Channel

    async def send(self, address: str, message: RenderedMessage) -> DeliveryResult: ...
