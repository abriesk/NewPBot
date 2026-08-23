"""Multi-step input state (IMPLEMENTATION.md §13.1).

A half-finished booking survives a restart because it lives in a row, not in
aiogram FSM memory. That is the whole point: `web` is redeployed and the client
carries on typing where they left off.

Channel-agnostic on purpose. Telegram uses it first, but the web wizard and any
future adapter get the same store keyed on their own channel, so two flows for
one person never collide.

`data` is scratch: half-typed answers, the slot under consideration. It can hold
problem text, so it is never logged (hard rule 8) and is cleared as soon as the
flow finishes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Channel
from app.core.models import FlowState
from app.core.policies import now_utc
from app.core.services.settings import get_practice


class Step(StrEnum):
    """Where a client is in a conversation.

    Stored as text, so adding a step is not a migration.
    """

    idle = "idle"
    choosing_language = "choosing_language"

    # Booking, in the order §13.1 lists them.
    choosing_timezone = "choosing_timezone"
    choosing_slot = "choosing_slot"
    choosing_session_type = "choosing_session_type"
    choosing_modality = "choosing_modality"
    entering_problem = "entering_problem"
    entering_name = "entering_name"
    entering_contact = "entering_contact"

    # Free-text negotiation path.
    entering_desired_time = "entering_desired_time"

    # Waitlist.
    waitlist_problem = "waitlist_problem"
    waitlist_contact = "waitlist_contact"


async def get(session: AsyncSession, client_id: UUID, channel: Channel) -> FlowState | None:
    return (
        await session.execute(
            select(FlowState).where(FlowState.client_id == client_id, FlowState.channel == channel)
        )
    ).scalar_one_or_none()


async def current_step(session: AsyncSession, client_id: UUID, channel: Channel) -> Step:
    state = await get(session, client_id, channel)
    if state is None:
        return Step.idle
    try:
        return Step(state.step)
    except ValueError:
        # A step removed by a deploy: drop the client back to the menu rather
        # than stranding them in a state nothing handles.
        return Step.idle


async def set_step(
    session: AsyncSession,
    client_id: UUID,
    channel: Channel,
    step: Step,
    *,
    merge: dict[str, Any] | None = None,
    replace: dict[str, Any] | None = None,
) -> FlowState:
    """Move to `step`, merging or replacing the scratch data."""
    practice = await get_practice(session)
    state = await get(session, client_id, channel)

    if state is None:
        state = FlowState(
            practice_id=practice.id,
            client_id=client_id,
            channel=channel,
            step=step.value,
            data=replace if replace is not None else (merge or {}),
        )
        session.add(state)
        await session.flush()
        return state

    state.step = step.value
    if replace is not None:
        state.data = replace
    elif merge:
        # Reassigned rather than mutated: SQLAlchemy does not track in-place
        # changes to a JSONB dict, and the update would be silently dropped.
        state.data = {**(state.data or {}), **merge}
    state.updated_at = now_utc()
    await session.flush()
    return state


async def remember(
    session: AsyncSession, client_id: UUID, channel: Channel, **values: Any
) -> FlowState:
    """Store answers without changing the step."""
    step = await current_step(session, client_id, channel)
    return await set_step(session, client_id, channel, step, merge=values)


async def data(session: AsyncSession, client_id: UUID, channel: Channel) -> dict[str, Any]:
    state = await get(session, client_id, channel)
    return dict(state.data or {}) if state else {}


async def clear(session: AsyncSession, client_id: UUID, channel: Channel) -> None:
    """Finish a flow. Called on submit, on cancel, and on /start."""
    await session.execute(
        delete(FlowState).where(FlowState.client_id == client_id, FlowState.channel == channel)
    )
    await session.flush()
