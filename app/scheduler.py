from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import select

from app.config import Settings
from app.database import SessionFactory
from app.models import (
    AssignmentStatus,
    CompletionPoll,
    FoodAssignment,
    GroupNotificationKind,
    NotificationKind,
    PollStatus,
    User,
)
from app.services import (
    active_queue,
    claim_notification,
    create_completion_poll,
    current_assignment,
    enqueue_group_notification,
    finish_group_notification,
    finish_notification,
    pending_group_notifications,
    poll_has_no_activity,
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

        # Create the 22:00 poll once and settle all polls whose 23:59 deadline passed.
        no_activity_assignment_ids: list[int] = []
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
                        assignment.duty_date == local_now.date() and local_now.time() >= time(22)
                    )
                    if not poll_time_reached:
                        continue
                    poll = await create_completion_poll(session, assignment, self.settings.timezone)
                    if poll.closes_at <= now:
                        if await poll_has_no_activity(session, poll):
                            no_activity_assignment_ids.append(assignment.id)
                        await resolve_poll(session, poll, now)
                        created_or_resolved = True
                        break
            if not created_or_resolved:
                break

        for assignment_id in set(no_activity_assignment_ids):
            await self._send_no_activity_alert(assignment_id)
        await self._queue_daily_group_announcement(local_now)
        await self._send_pending_group_notifications()
        await self._send_open_poll_messages(now)
        await self._send_due_reminders(local_now)

    async def _send_due_reminders(self, local_now: datetime) -> None:
        reminder_slots = (
            (time(7), NotificationKind.MORNING),
            (time(12), NotificationKind.NOON),
            (time(19), NotificationKind.EVENING),
        )
        due_slots = [(scheduled_at, kind) for scheduled_at, kind in reminder_slots if scheduled_at <= local_now.time()]
        if not due_slots:
            return
        # On recovery, deliver only the newest outstanding reminder instead of sending
        # three stale, identical messages at once.
        _, kind = due_slots[-1]
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
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Ovqat tayyorladim", callback_data=f"duty:done:{assignment.id}")]
                ]
            )
            await self._send_and_log(
                session,
                assignment.id,
                kind,
                user,
                "🍽 Bugun sizning ovqat navbatingiz. Iltimos, ovqatni tayyorlang.",
                reply_markup=markup,
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
                        f"Bugun {assignee.full_name} ovqat qildimi?\nOvoz berish 23:59 da yopiladi.",
                        reply_markup=markup,
                    )

    async def _queue_daily_group_announcement(self, local_now: datetime) -> None:
        if local_now.time() < time(7):
            return
        async with SessionFactory() as session:
            assignment = await current_assignment(session, local_now.date())
            if assignment is None:
                return
            await enqueue_group_notification(session, assignment, GroupNotificationKind.DAILY_DUTY)
            await session.commit()

    async def _send_pending_group_notifications(self) -> None:
        failed_assignment_ids: set[int] = set()
        async with SessionFactory() as session:
            for log in await pending_group_notifications(session, utc_now()):
                target = await session.get(User, log.target_user_id)
                if target is None:
                    await finish_group_notification(session, log, "Duty user no longer exists", terminal=True)
                    failed_assignment_ids.add(log.assignment_id)
                    continue
                mention = f'<a href="tg://user?id={target.telegram_id}">{escape(target.full_name)}</a>'
                text = (
                    f"🍽 {mention}, bugun sizning ovqat navbatingiz."
                    if log.kind == GroupNotificationKind.DAILY_DUTY
                    else f"🔄 Bugungi ovqat navbati o‘zgardi: {mention} endi navbatchi."
                )
                try:
                    await self.bot.send_message(log.chat_id, text, parse_mode=ParseMode.HTML)
                except TelegramForbiddenError as error:
                    await finish_group_notification(session, log, str(error), terminal=True)
                    failed_assignment_ids.add(log.assignment_id)
                except TelegramAPIError as error:
                    await finish_group_notification(session, log, str(error))
                    logger.warning("Could not send group notification to %s: %s", log.chat_id, error)
                else:
                    await finish_group_notification(session, log)
        for assignment_id in failed_assignment_ids:
            await self._send_group_error_alert(assignment_id)

    async def _send_no_activity_alert(self, assignment_id: int) -> None:
        async with SessionFactory() as session:
            assignment = await session.get(FoodAssignment, assignment_id)
            if assignment is None:
                return
            assignee = await session.get(User, assignment.assigned_user_id)
            admins = list((await session.scalars(select(User).where(User.is_admin.is_(True)))).all())
            for admin in admins:
                claimed = await claim_notification(session, assignment.id, NotificationKind.ADMIN_ALERT, admin.id)
                if not claimed:
                    continue
                await self._send_and_log(
                    session,
                    assignment.id,
                    NotificationKind.ADMIN_ALERT,
                    admin,
                    f"⚠️ {assignee.full_name if assignee else 'Navbatchi'} uchun hech kim ovoz bermadi "
                    "va u ham ovqat tayyorlaganini tasdiqlamadi. Navbat ertaga takrorlanadi.",
                )

    async def _send_group_error_alert(self, assignment_id: int) -> None:
        async with SessionFactory() as session:
            assignment = await session.get(FoodAssignment, assignment_id)
            if assignment is None:
                return
            admins = list((await session.scalars(select(User).where(User.is_admin.is_(True)))).all())
            for admin in admins:
                claimed = await claim_notification(session, assignment.id, NotificationKind.GROUP_ERROR, admin.id)
                if not claimed:
                    continue
                await self._send_and_log(
                    session,
                    assignment.id,
                    NotificationKind.GROUP_ERROR,
                    admin,
                    "⚠️ Guruhga navbatchi e’loni yuborilmadi. Botni guruhga qayta qo‘shing va yozish huquqini tekshiring.",
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
