from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import select

from app.config import Settings
from app.database import SessionFactory
from app.models import (
    AssignmentStatus,
    CompletionPoll,
    FoodAssignment,
    NotificationKind,
    PollStatus,
    User,
)
from app.services import (
    active_queue,
    claim_notification,
    create_completion_poll,
    current_assignment,
    finish_notification,
    resolve_poll,
    utc_now,
)

logger = logging.getLogger(__name__)


class DutyScheduler:
    """Database-backed scheduler; run exactly one bot process for this MVP."""

    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings
        self.zone = ZoneInfo(settings.timezone)
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("Duty scheduler tick failed")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=30)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopped.set()

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utc_now()
        local_now = now.astimezone(self.zone)

        # Recover from a short outage: create missing closing polls and settle every expired one.
        for _ in range(32):
            created_or_resolved = False
            async with SessionFactory() as session:
                result = await session.scalars(
                    select(FoodAssignment)
                    .where(FoodAssignment.status == AssignmentStatus.ACTIVE)
                    .order_by(FoodAssignment.duty_date)
                )
                assignments = list(result)
                for assignment in assignments:
                    poll_time_reached = assignment.duty_date < local_now.date() or (
                        assignment.duty_date == local_now.date() and local_now.time() >= time(23, 59)
                    )
                    if not poll_time_reached:
                        continue
                    poll = await create_completion_poll(session, assignment, self.settings.timezone)
                    if poll.closes_at <= now:
                        await resolve_poll(session, poll, now)
                        created_or_resolved = True
                        break
            if not created_or_resolved:
                break

        await self._send_open_poll_messages(now)
        await self._send_due_reminders(local_now)

    async def _send_due_reminders(self, local_now: datetime) -> None:
        reminder_hours = {
            7: NotificationKind.MORNING,
            12: NotificationKind.NOON,
            19: NotificationKind.EVENING,
        }
        kind = reminder_hours.get(local_now.hour)
        # A task is due for the full hour. The log guarantees it is sent only once.
        if kind is None:
            return
        async with SessionFactory() as session:
            assignment = await current_assignment(session, local_now.date())
            if assignment is None:
                return
            user = await session.get(User, assignment.assigned_user_id)
            if user is None:
                return
            claimed = await claim_notification(session, assignment.id, kind, user.id)
            if not claimed:
                return
            await self._send_and_log(
                session,
                assignment.id,
                kind,
                user,
                "🍽 Bugun sizning ovqat navbatingiz. Iltimos, ovqatni tayyorlang.",
            )

    async def _send_open_poll_messages(self, now: datetime) -> None:
        async with SessionFactory() as session:
            polls = await session.scalars(
                select(CompletionPoll).where(CompletionPoll.status == PollStatus.OPEN, CompletionPoll.closes_at > now)
            )
            for poll in polls:
                assignment = await session.get(FoodAssignment, poll.assignment_id)
                if assignment is None:
                    continue
                assignee = await session.get(User, assignment.assigned_user_id)
                if assignee is None:
                    continue
                for _, voter in await active_queue(session):
                    if voter.id == assignee.id:
                        continue
                    claimed = await claim_notification(session, assignment.id, NotificationKind.POLL, voter.id)
                    if not claimed:
                        continue
                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                    markup = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(text="✅ Ha", callback_data=f"vote:{poll.id}:yes"),
                                InlineKeyboardButton(text="❌ Yo‘q", callback_data=f"vote:{poll.id}:no"),
                            ]
                        ]
                    )
                    await self._send_and_log(
                        session,
                        assignment.id,
                        NotificationKind.POLL,
                        voter,
                        f"Bugun {assignee.full_name} ovqat qildimi?\nOvoz berish 00:15 da yopiladi.",
                        reply_markup=markup,
                    )

    async def _send_and_log(self, session, assignment_id, kind, user, text, reply_markup=None) -> None:
        try:
            await self.bot.send_message(user.telegram_id, text, reply_markup=reply_markup)
        except TelegramForbiddenError as error:
            await finish_notification(session, assignment_id, kind, user.id, str(error), terminal=True)
            logger.warning("User %s blocked the bot", user.telegram_id)
        except TelegramAPIError as error:
            await finish_notification(session, assignment_id, kind, user.id, str(error))
            logger.warning("Could not send notification to %s: %s", user.telegram_id, error)
        else:
            await finish_notification(session, assignment_id, kind, user.id)
