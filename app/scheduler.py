from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.config import Settings
from app.database import SessionFactory
from app.models import (
    AssignmentStatus,
    CompletionPoll,
    FoodAssignment,
    GroupNotificationKind,
    ManualReminderChannel,
    NotificationKind,
    PollStatus,
    SupplyNotificationKind,
    SupplyPollStatus,
    SupplyTask,
    SupplyTaskStatus,
    SupplyType,
    SupplyVerificationPoll,
    User,
)
from app.services import (
    active_queue,
    active_room,
    active_room_users,
    claim_manual_reminder,
    claim_notification,
    claim_supply_notification,
    create_completion_poll,
    current_assignment,
    enqueue_group_notification,
    expire_supply_transfers,
    finish_group_notification,
    finish_manual_reminder,
    finish_notification,
    finish_supply_notification,
    pending_group_notifications,
    pending_manual_reminder_log_ids,
    poll_has_no_activity,
    resolve_poll,
    resolve_supply_poll,
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
        await self._expire_supply_transfers(now)
        await self._resolve_expired_supply_polls(now)
        await self._send_supply_task_notifications()
        await self._send_supply_poll_messages(now)
        await self._send_open_poll_messages(now)
        await self._send_due_reminders(local_now)
        await self.dispatch_manual_reminders()

    async def dispatch_manual_reminders(self, log_ids: list[int] | None = None) -> tuple[int, int]:
        """Deliver manual-reminder outbox records now; retries are handled by tick()."""
        async with SessionFactory() as session:
            pending_ids = await pending_manual_reminder_log_ids(session, log_ids=log_ids)

        delivered = 0
        failed = 0
        for log_id in pending_ids:
            async with SessionFactory() as session:
                log = await claim_manual_reminder(session, log_id)
                if log is None:
                    continue
                recipient = await session.get(User, log.recipient_user_id)
                if recipient is None:
                    await finish_manual_reminder(session, log, "Recipient no longer exists", terminal=True)
                    failed += 1
                    continue

                markup = None
                parse_mode = None
                if log.food_assignment_id is not None:
                    assignment = await session.get(FoodAssignment, log.food_assignment_id)
                    is_current = bool(
                        assignment
                        and assignment.status == AssignmentStatus.ACTIVE
                        and assignment.assigned_user_id == recipient.id
                        and assignment.reported_done_at is None
                    )
                    direct_text = "🔔 Eslatma\n\nSizda bugungi ovqat navbati bor. Iltimos, bajargach tasdiqlang."
                    group_text = (
                        f'🔔 Eslatma: <a href="tg://user?id={recipient.telegram_id}">'
                        f"{escape(recipient.full_name)}</a>, bugungi ovqat navbati sizda."
                    )
                    markup = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✅ Ovqat tayyorladim", callback_data=f"duty:done:{log.food_assignment_id}"
                                )
                            ]
                        ]
                    )
                else:
                    task = await session.get(SupplyTask, log.supply_task_id)
                    is_current = bool(
                        task
                        and task.status == SupplyTaskStatus.AWAITING_DELIVERY
                        and task.assigned_user_id == recipient.id
                    )
                    label = self._supply_label(task.supply_type) if task else "Ta’minot"
                    direct_text = f"🔔 Eslatma\n\n{label} olib kelish navbati sizda. Iltimos, bajargach tasdiqlang."
                    group_text = (
                        f'🔔 Eslatma: <a href="tg://user?id={recipient.telegram_id}">'
                        f"{escape(recipient.full_name)}</a>, {label.lower()} olib kelish navbati sizda."
                    )
                    markup = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✅ Olib keldim", callback_data=f"supply:done:{log.supply_task_id}"
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="🔄 Navbatni o‘tkazish",
                                    callback_data=f"supply:transfer:{log.supply_task_id}",
                                )
                            ],
                        ]
                    )
                if not is_current:
                    await finish_manual_reminder(session, log, "Target is no longer active", terminal=True)
                    failed += 1
                    continue

                if log.channel == ManualReminderChannel.DIRECT:
                    chat_id = recipient.telegram_id
                    text = direct_text
                else:
                    room = await active_room(session)
                    if room is None or room.chat_id != log.chat_id:
                        await finish_manual_reminder(session, log, "Active group changed or disconnected", terminal=True)
                        failed += 1
                        continue
                    chat_id = log.chat_id
                    text = group_text
                    markup = None
                    parse_mode = ParseMode.HTML

                try:
                    await self.bot.send_message(chat_id, text, reply_markup=markup, parse_mode=parse_mode)
                except TelegramForbiddenError as error:
                    await finish_manual_reminder(session, log, str(error), terminal=True)
                    failed += 1
                except TelegramAPIError as error:
                    await finish_manual_reminder(session, log, str(error))
                    logger.warning("Could not send manual reminder %s: %s", log.id, error)
                    failed += 1
                else:
                    await finish_manual_reminder(session, log)
                    delivered += 1
        return delivered, failed

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

    @staticmethod
    def _supply_label(supply_type: SupplyType) -> str:
        return "Non" if supply_type == SupplyType.BREAD else "Suv"

    async def _resolve_expired_supply_polls(self, now: datetime) -> None:
        async with SessionFactory() as session:
            polls = list(
                (
                    await session.scalars(
                        select(SupplyVerificationPoll).where(
                            SupplyVerificationPoll.status == SupplyPollStatus.OPEN,
                            SupplyVerificationPoll.closes_at <= now,
                        )
                    )
                ).all()
            )
            for poll in polls:
                await resolve_supply_poll(session, poll, now)

    async def _expire_supply_transfers(self, now: datetime) -> None:
        async with SessionFactory() as session:
            requests = await expire_supply_transfers(session, now)
            senders = [await session.get(User, request.from_user_id) for request in requests]
        for sender in senders:
            if sender is None:
                continue
            try:
                await self.bot.send_message(
                    sender.telegram_id,
                    "Ta’minot navbatini o‘tkazish so‘rovi 15 daqiqada qabul qilinmadi. Navbat sizda qoldi.",
                )
            except TelegramAPIError:
                logger.warning("Could not send expired transfer notification to %s", sender.telegram_id)

    async def _send_supply_task_notifications(self) -> None:
        async with SessionFactory() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(SupplyTask).where(
                            SupplyTask.status.in_((SupplyTaskStatus.AWAITING_DELIVERY, SupplyTaskStatus.VERIFYING))
                        )
                    )
                ).all()
            )
            room = await active_room(session)
            for task in tasks:
                assignee = await session.get(User, task.assigned_user_id)
                if assignee is None:
                    continue
                label = self._supply_label(task.supply_type)
                revision_key = f"assignment:{task.notification_revision}"
                direct_log = await claim_supply_notification(
                    session,
                    task.id,
                    SupplyNotificationKind.ASSIGNMENT_DIRECT,
                    revision_key,
                    f"dm:{assignee.id}",
                    recipient_user_id=assignee.id,
                )
                if direct_log:
                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                    markup = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Olib keldim", callback_data=f"supply:done:{task.id}")],
                            [InlineKeyboardButton(text="🔄 Navbatni o‘tkazish", callback_data=f"supply:transfer:{task.id}")],
                        ]
                    )
                    await self._send_supply_log(
                        session,
                        direct_log,
                        assignee.telegram_id,
                        f"{label} tugadi. Sizning navbatingiz — olib kelishingiz kerak.",
                        reply_markup=markup,
                    )
                if room is None:
                    continue
                group_log = await claim_supply_notification(
                    session,
                    task.id,
                    SupplyNotificationKind.ASSIGNMENT_GROUP,
                    revision_key,
                    f"group:{room.chat_id}",
                    chat_id=room.chat_id,
                )
                if group_log:
                    mention = f'<a href="tg://user?id={assignee.telegram_id}">{escape(assignee.full_name)}</a>'
                    if task.notification_revision == 0:
                        text = f"{label} tugadi. {mention}, sizning navbatingiz — olib kelishingiz kerak."
                    else:
                        previous = await session.get(User, task.previous_assignee_user_id)
                        old_mention = (
                            f'<a href="tg://user?id={previous.telegram_id}">{escape(previous.full_name)}</a>'
                            if previous
                            else "Oldingi navbatchi"
                        )
                        text = f"🔄 {label} navbati {old_mention} dan {mention} ga o‘tdi."
                    await self._send_supply_log(session, group_log, room.chat_id, text, parse_mode=ParseMode.HTML)

    async def _send_supply_poll_messages(self, now: datetime) -> None:
        async with SessionFactory() as session:
            polls = list(
                (
                    await session.scalars(
                        select(SupplyVerificationPoll).where(
                            SupplyVerificationPoll.status == SupplyPollStatus.OPEN,
                            SupplyVerificationPoll.closes_at > now,
                        )
                    )
                ).all()
            )
            voters = await active_room_users(session)
            for poll in polls:
                task = await session.get(SupplyTask, poll.task_id)
                if task is None:
                    continue
                assignee = await session.get(User, task.assigned_user_id)
                if assignee is None:
                    continue
                label = self._supply_label(task.supply_type)
                for voter in voters:
                    if voter.id == assignee.id:
                        continue
                    log = await claim_supply_notification(
                        session,
                        task.id,
                        SupplyNotificationKind.POLL,
                        f"poll:{poll.id}",
                        f"dm:{voter.id}",
                        recipient_user_id=voter.id,
                        poll_id=poll.id,
                    )
                    if log is None:
                        continue
                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                    markup = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(text="✅ Ha", callback_data=f"supplyvote:{poll.id}:yes"),
                                InlineKeyboardButton(text="❌ Yo‘q", callback_data=f"supplyvote:{poll.id}:no"),
                            ]
                        ]
                    )
                    await self._send_supply_log(
                        session,
                        log,
                        voter.telegram_id,
                        f"{assignee.full_name} {label.lower()} olib keldimi? Ovoz 30 daqiqada yopiladi.",
                        reply_markup=markup,
                    )

    async def _send_supply_log(self, session, log, chat_id: int, text: str, reply_markup=None, parse_mode=None) -> None:
        try:
            await self.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        except TelegramForbiddenError as error:
            await finish_supply_notification(session, log, str(error), terminal=True)
        except TelegramAPIError as error:
            await finish_supply_notification(session, log, str(error))
            logger.warning("Could not send supply notification to %s: %s", chat_id, error)
        else:
            await finish_supply_notification(session, log)

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
