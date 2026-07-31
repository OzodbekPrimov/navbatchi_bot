from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Select, delete, func, select, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    AssignmentStatus,
    CompletionPoll,
    FoodAssignment,
    FoodQueueMember,
    GroupNotificationKind,
    GroupNotificationLog,
    ManualReminderChannel,
    ManualReminderLog,
    NotificationKind,
    NotificationLog,
    PollStatus,
    PollVote,
    RoomSetting,
    SupplyActiveTask,
    SupplyNotificationKind,
    SupplyNotificationLog,
    SupplyPollStatus,
    SupplyPollVote,
    SupplyQueueMember,
    SupplyRotationState,
    SupplyTask,
    SupplyTaskStatus,
    SupplyTransferRequest,
    SupplyTransferStatus,
    SupplyType,
    SupplyVerificationPoll,
    TransferRequest,
    TransferStatus,
    User,
    VoteValue,
)


class DomainError(Exception):
    """A business-rule error that can be shown to a bot user."""


MANUAL_REMINDER_COOLDOWN = timedelta(minutes=30)


@dataclass(frozen=True)
class ManualReminderTarget:
    kind: str
    reference_id: int
    user_id: int
    user_name: str
    label: str
    cooldown_until: datetime | None


@dataclass(frozen=True)
class ManualReminderQueueResult:
    log_ids: list[int]
    target_count: int
    skipped_cooldown_count: int


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes even for timezone-aware columns."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    full_name: str,
    username: str | None,
    is_admin: bool,
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(
            telegram_id=telegram_id,
            full_name=full_name[:255],
            username=username[:255] if username else None,
            is_admin=is_admin,
        )
        session.add(user)
    else:
        user.full_name = full_name[:255]
        user.username = username[:255] if username else None
        # ADMIN_IDS is the source of truth.  In particular, removing an ID from
        # the configuration must revoke its old database-level permission.
        user.is_admin = is_admin
    await session.commit()
    return user


async def food_history_page(
    session: AsyncSession, *, offset: int = 0, limit: int = 10
) -> tuple[list[tuple[FoodAssignment, User, User]], bool]:
    """Return resolved food duties with the planned and effective assignees."""
    scheduled_user = aliased(User)
    assigned_user = aliased(User)
    rows = list(
        (
            await session.execute(
                select(FoodAssignment, scheduled_user, assigned_user)
                .join(scheduled_user, scheduled_user.id == FoodAssignment.scheduled_user_id)
                .join(assigned_user, assigned_user.id == FoodAssignment.assigned_user_id)
                .where(FoodAssignment.status != AssignmentStatus.ACTIVE)
                .order_by(FoodAssignment.duty_date.desc(), FoodAssignment.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
    )
    return rows[:limit], len(rows) > limit


async def supply_history_page(
    session: AsyncSession, supply_type: SupplyType, *, offset: int = 0, limit: int = 10
) -> tuple[list[tuple[SupplyTask, User, User, User]], bool]:
    """Return completed supply duties with requester, planned and actual users."""
    requester = aliased(User)
    scheduled_user = aliased(User)
    assigned_user = aliased(User)
    rows = list(
        (
            await session.execute(
                select(SupplyTask, requester, scheduled_user, assigned_user)
                .join(requester, requester.id == SupplyTask.requester_user_id)
                .join(scheduled_user, scheduled_user.id == SupplyTask.scheduled_user_id)
                .join(assigned_user, assigned_user.id == SupplyTask.assigned_user_id)
                .where(
                    SupplyTask.supply_type == supply_type,
                    SupplyTask.status == SupplyTaskStatus.COMPLETED,
                )
                .order_by(SupplyTask.completed_at.desc(), SupplyTask.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
    )
    return rows[:limit], len(rows) > limit


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def is_queue_member(session: AsyncSession, user_id: int) -> bool:
    return bool(
        await session.scalar(
            select(FoodQueueMember.id).where(
                FoodQueueMember.user_id == user_id, FoodQueueMember.is_active.is_(True)
            )
        )
    )


async def active_queue(session: AsyncSession) -> list[tuple[FoodQueueMember, User]]:
    result = await session.execute(
        select(FoodQueueMember, User)
        .join(User, User.id == FoodQueueMember.user_id)
        .where(FoodQueueMember.is_active.is_(True))
        .order_by(FoodQueueMember.position)
    )
    return list(result.all())


async def add_queue_member(session: AsyncSession, user_id: int) -> None:
    if await is_queue_member(session, user_id):
        raise DomainError("Bu foydalanuvchi ovqat navbatida bor.")
    user = await session.get(User, user_id)
    if user is None:
        raise DomainError("Foydalanuvchi topilmadi.")
    existing = await session.scalar(select(FoodQueueMember).where(FoodQueueMember.user_id == user_id))
    next_position = (
        await session.scalar(select(func.max(FoodQueueMember.position)).where(FoodQueueMember.is_active.is_(True)))
    ) or 0
    if existing is not None:
        existing.is_active = True
        existing.position = next_position + 1
    else:
        session.add(FoodQueueMember(user_id=user_id, position=next_position + 1))
    await session.commit()


async def _get_member(session: AsyncSession, user_id: int, active_only: bool = True) -> FoodQueueMember | None:
    statement: Select[tuple[FoodQueueMember]] = select(FoodQueueMember).where(FoodQueueMember.user_id == user_id)
    if active_only:
        statement = statement.where(FoodQueueMember.is_active.is_(True))
    return await session.scalar(statement)


async def remove_queue_member(session: AsyncSession, user_id: int) -> None:
    member = await _get_member(session, user_id)
    if member is None:
        raise DomainError("Bu foydalanuvchi faol navbatda emas.")
    current = await session.scalar(
        select(FoodAssignment).where(FoodAssignment.status == AssignmentStatus.ACTIVE)
    )
    if current and user_id in {current.assigned_user_id, current.scheduled_user_id}:
        raise DomainError("Bugungi navbatdagi odamni avval almashtiring, keyin ro‘yxatdan o‘chiring.")
    member.is_active = False
    # Inactive members must not block the unique order positions of active members.
    member.position = -member.id
    await session.commit()


async def move_queue_member(session: AsyncSession, user_id: int, direction: int) -> None:
    entries = await active_queue(session)
    index = next((i for i, (member, _) in enumerate(entries) if member.user_id == user_id), None)
    if index is None:
        raise DomainError("Foydalanuvchi faol navbatda emas.")
    other_index = index + direction
    if other_index < 0 or other_index >= len(entries):
        return
    entries[index], entries[other_index] = entries[other_index], entries[index]
    # Avoid a temporary unique-position collision while rewriting the order.
    for member, _ in entries:
        member.position = -(10_000 + member.id)
    await session.flush()
    for position, (member, _) in enumerate(entries, start=1):
        member.position = position
    await session.commit()


async def _next_queue_user(session: AsyncSession, scheduled_user_id: int) -> int:
    current = await _get_member(session, scheduled_user_id, active_only=False)
    active = await active_queue(session)
    if not active:
        raise DomainError("Ovqat navbati bo‘sh.")
    if current:
        for member, _ in active:
            if member.position > current.position:
                return member.user_id
    return active[0][0].user_id


async def get_assignment_for_date(session: AsyncSession, duty_date: date) -> FoodAssignment | None:
    return await session.scalar(select(FoodAssignment).where(FoodAssignment.duty_date == duty_date))


async def create_initial_assignment(session: AsyncSession, duty_date: date, user_id: int) -> FoodAssignment:
    if not await is_queue_member(session, user_id):
        raise DomainError("Boshlang‘ich navbatchi ovqat navbatida bo‘lishi kerak.")
    assignment = await get_assignment_for_date(session, duty_date)
    if assignment is not None:
        raise DomainError("Bugungi navbat allaqachon yaratilgan.")
    assignment = FoodAssignment(duty_date=duty_date, scheduled_user_id=user_id, assigned_user_id=user_id)
    session.add(assignment)
    await session.commit()
    return assignment


async def current_assignment(session: AsyncSession, duty_date: date) -> FoodAssignment | None:
    return await session.scalar(
        select(FoodAssignment).where(
            FoodAssignment.duty_date == duty_date, FoodAssignment.status == AssignmentStatus.ACTIVE
        )
    )


async def set_active_room(
    session: AsyncSession, chat_id: int, title: str | None, configured_by_user_id: int
) -> RoomSetting:
    """Bind this single-room MVP to a group, replacing any previously active group."""
    active_rooms = await session.scalars(select(RoomSetting).where(RoomSetting.is_active.is_(True)))
    for room in active_rooms:
        room.is_active = False
    room = await session.scalar(select(RoomSetting).where(RoomSetting.chat_id == chat_id))
    if room is None:
        room = RoomSetting(
            chat_id=chat_id,
            title=title[:255] if title else None,
            configured_by_user_id=configured_by_user_id,
        )
        session.add(room)
    else:
        room.is_active = True
        room.title = title[:255] if title else room.title
        room.configured_by_user_id = configured_by_user_id
    await session.commit()
    return room


async def deactivate_room(session: AsyncSession, chat_id: int) -> bool:
    room = await session.scalar(
        select(RoomSetting).where(RoomSetting.chat_id == chat_id, RoomSetting.is_active.is_(True))
    )
    if room is None:
        return False
    room.is_active = False
    await session.commit()
    return True


async def active_room(session: AsyncSession) -> RoomSetting | None:
    return await session.scalar(
        select(RoomSetting).where(RoomSetting.is_active.is_(True)).order_by(RoomSetting.id.desc())
    )


async def _manual_reminder_cooldown_until(
    session: AsyncSession, kind: str, reference_id: int, now: datetime
) -> datetime | None:
    target_column = (
        ManualReminderLog.food_assignment_id if kind == "food" else ManualReminderLog.supply_task_id
    )
    latest = await session.scalar(
        select(ManualReminderLog.created_at)
        .where(target_column == reference_id, ManualReminderLog.created_at > now - MANUAL_REMINDER_COOLDOWN)
        .order_by(ManualReminderLog.created_at.desc())
        .limit(1)
    )
    return _as_utc(latest) + MANUAL_REMINDER_COOLDOWN if latest else None


async def active_manual_reminder_targets(
    session: AsyncSession, duty_date: date, now: datetime | None = None
) -> list[ManualReminderTarget]:
    """Return reminder-eligible tasks and their per-task cooldown state."""
    now = now or utc_now()
    targets: list[ManualReminderTarget] = []
    assignment = await current_assignment(session, duty_date)
    if assignment and assignment.reported_done_at is None:
        assignee = await session.get(User, assignment.assigned_user_id)
        if assignee:
            targets.append(
                ManualReminderTarget(
                    kind="food",
                    reference_id=assignment.id,
                    user_id=assignee.id,
                    user_name=assignee.full_name,
                    label="🍽 Ovqat",
                    cooldown_until=await _manual_reminder_cooldown_until(session, "food", assignment.id, now),
                )
            )

    tasks = list(
        (
            await session.scalars(
                select(SupplyTask)
                .join(SupplyActiveTask, SupplyActiveTask.task_id == SupplyTask.id)
                .where(SupplyTask.status == SupplyTaskStatus.AWAITING_DELIVERY)
                .order_by(SupplyTask.supply_type)
            )
        ).all()
    )
    for task in tasks:
        assignee = await session.get(User, task.assigned_user_id)
        if assignee is None:
            continue
        label = "🥖 Non" if task.supply_type == SupplyType.BREAD else "💧 Suv"
        targets.append(
            ManualReminderTarget(
                kind="supply",
                reference_id=task.id,
                user_id=assignee.id,
                user_name=assignee.full_name,
                label=label,
                cooldown_until=await _manual_reminder_cooldown_until(session, "supply", task.id, now),
            )
        )
    return targets


async def queue_manual_reminders(
    session: AsyncSession,
    initiator_user_id: int,
    duty_date: date,
    references: list[tuple[str, int]],
    now: datetime | None = None,
) -> ManualReminderQueueResult:
    """Create direct and group outbox rows, enforcing the reminder cooldown."""
    now = now or utc_now()
    room = await active_room(session)
    if room is None:
        raise DomainError("Avval faol Telegram guruhini ulang.")
    active_targets = {
        (target.kind, target.reference_id): target
        for target in await active_manual_reminder_targets(session, duty_date, now)
    }
    unique_references = list(dict.fromkeys(references))
    if not unique_references:
        raise DomainError("Eslatish uchun faol navbatchi yo‘q.")
    missing = [reference for reference in unique_references if reference not in active_targets]
    if missing:
        raise DomainError("Tanlangan navbatchi endi eslatish uchun faol emas.")

    log_ids: list[int] = []
    skipped = 0
    for reference in unique_references:
        target = active_targets[reference]
        if target.cooldown_until and target.cooldown_until > now:
            skipped += 1
            continue
        food_assignment_id = target.reference_id if target.kind == "food" else None
        supply_task_id = target.reference_id if target.kind == "supply" else None
        direct_log = ManualReminderLog(
            initiated_by_user_id=initiator_user_id,
            recipient_user_id=target.user_id,
            food_assignment_id=food_assignment_id,
            supply_task_id=supply_task_id,
            channel=ManualReminderChannel.DIRECT,
            created_at=now,
        )
        group_log = ManualReminderLog(
            initiated_by_user_id=initiator_user_id,
            recipient_user_id=target.user_id,
            food_assignment_id=food_assignment_id,
            supply_task_id=supply_task_id,
            channel=ManualReminderChannel.GROUP,
            chat_id=room.chat_id,
            created_at=now,
        )
        session.add_all((direct_log, group_log))
        await session.flush()
        log_ids.extend((direct_log.id, group_log.id))
    await session.commit()
    return ManualReminderQueueResult(
        log_ids=log_ids,
        target_count=len(log_ids) // 2,
        skipped_cooldown_count=skipped,
    )


async def pending_manual_reminder_log_ids(
    session: AsyncSession, now: datetime | None = None, log_ids: list[int] | None = None
) -> list[int]:
    now = now or utc_now()
    conditions = [
        ManualReminderLog.sent_at.is_(None),
        ManualReminderLog.is_terminal.is_(False),
        (ManualReminderLog.next_attempt_at.is_(None) | (ManualReminderLog.next_attempt_at <= now)),
    ]
    if log_ids is not None:
        conditions.append(ManualReminderLog.id.in_(log_ids))
    return list((await session.scalars(select(ManualReminderLog.id).where(*conditions))).all())


async def claim_manual_reminder(
    session: AsyncSession, log_id: int, now: datetime | None = None
) -> ManualReminderLog | None:
    now = now or utc_now()
    log = await session.scalar(
        select(ManualReminderLog).where(ManualReminderLog.id == log_id).with_for_update()
    )
    if log is None or log.sent_at or log.is_terminal or (
        log.next_attempt_at and _as_utc(log.next_attempt_at) > now
    ):
        return None
    # Reserve the record while Telegram is called so two scheduler ticks cannot send it twice.
    log.next_attempt_at = now + timedelta(minutes=2)
    await session.commit()
    return log


async def finish_manual_reminder(
    session: AsyncSession,
    log: ManualReminderLog,
    error: str | None = None,
    terminal: bool = False,
) -> None:
    if error:
        log.error = error
        log.attempts += 1
        log.is_terminal = terminal
        log.next_attempt_at = utc_now() + timedelta(minutes=min(5 * log.attempts, 60))
    else:
        log.sent_at = utc_now()
        log.error = None
        log.next_attempt_at = None
    await session.commit()


async def enqueue_group_notification(
    session: AsyncSession, assignment: FoodAssignment, kind: GroupNotificationKind
) -> GroupNotificationLog | None:
    """Add a durable group outbox item. It is safe to call repeatedly."""
    room = await active_room(session)
    if room is None:
        return None
    if kind == GroupNotificationKind.DAILY_DUTY:
        existing = await session.scalar(
            select(GroupNotificationLog).where(
                GroupNotificationLog.assignment_id == assignment.id,
                GroupNotificationLog.chat_id == room.chat_id,
                GroupNotificationLog.kind == GroupNotificationKind.DAILY_DUTY,
            )
        )
    else:
        existing = await session.scalar(
            select(GroupNotificationLog).where(
                GroupNotificationLog.assignment_id == assignment.id,
                GroupNotificationLog.chat_id == room.chat_id,
                GroupNotificationLog.kind == kind,
                GroupNotificationLog.revision == assignment.notification_revision,
            )
        )
    if existing is not None:
        return existing
    log = GroupNotificationLog(
        assignment_id=assignment.id,
        chat_id=room.chat_id,
        target_user_id=assignment.assigned_user_id,
        kind=kind,
        revision=assignment.notification_revision,
    )
    session.add(log)
    await session.flush()
    return log


async def active_supply_queue(session: AsyncSession, supply_type: SupplyType) -> list[tuple[SupplyQueueMember, User]]:
    result = await session.execute(
        select(SupplyQueueMember, User)
        .join(User, User.id == SupplyQueueMember.user_id)
        .where(SupplyQueueMember.supply_type == supply_type, SupplyQueueMember.is_active.is_(True))
        .order_by(SupplyQueueMember.position)
    )
    return list(result.all())


async def is_supply_queue_member(session: AsyncSession, supply_type: SupplyType, user_id: int) -> bool:
    return bool(
        await session.scalar(
            select(SupplyQueueMember.id).where(
                SupplyQueueMember.supply_type == supply_type,
                SupplyQueueMember.user_id == user_id,
                SupplyQueueMember.is_active.is_(True),
            )
        )
    )


async def add_supply_queue_member(session: AsyncSession, supply_type: SupplyType, user_id: int) -> None:
    if await is_supply_queue_member(session, supply_type, user_id):
        raise DomainError("Bu foydalanuvchi ushbu navbatda bor.")
    member = await session.scalar(
        select(SupplyQueueMember).where(
            SupplyQueueMember.supply_type == supply_type, SupplyQueueMember.user_id == user_id
        )
    )
    last_position = await session.scalar(
        select(func.max(SupplyQueueMember.position)).where(
            SupplyQueueMember.supply_type == supply_type, SupplyQueueMember.is_active.is_(True)
        )
    ) or 0
    if member is None:
        session.add(SupplyQueueMember(supply_type=supply_type, user_id=user_id, position=last_position + 1))
    else:
        member.is_active = True
        member.position = last_position + 1
    await session.commit()


async def remove_supply_queue_member(session: AsyncSession, supply_type: SupplyType, user_id: int) -> None:
    member = await session.scalar(
        select(SupplyQueueMember).where(
            SupplyQueueMember.supply_type == supply_type,
            SupplyQueueMember.user_id == user_id,
            SupplyQueueMember.is_active.is_(True),
        )
    )
    if member is None:
        raise DomainError("Bu foydalanuvchi ushbu navbatda yo‘q.")
    active_task = await session.get(SupplyActiveTask, supply_type)
    if active_task:
        task = await session.get(SupplyTask, active_task.task_id)
        if task and user_id in {task.scheduled_user_id, task.assigned_user_id}:
            raise DomainError("Ochiq vazifadagi odamni avval almashtiring.")
    state = await session.get(SupplyRotationState, supply_type)
    if state and state.current_user_id == user_id:
        raise DomainError("Avval keyingi navbatchini belgilang.")
    member.is_active = False
    member.position = -member.id
    await session.commit()


async def move_supply_queue_member(
    session: AsyncSession, supply_type: SupplyType, user_id: int, direction: int
) -> None:
    entries = await active_supply_queue(session, supply_type)
    index = next((i for i, (member, _) in enumerate(entries) if member.user_id == user_id), None)
    if index is None:
        raise DomainError("Foydalanuvchi ushbu navbatda yo‘q.")
    other_index = index + direction
    if other_index < 0 or other_index >= len(entries):
        return
    entries[index], entries[other_index] = entries[other_index], entries[index]
    for member, _ in entries:
        member.position = -(20_000 + member.id)
    await session.flush()
    for position, (member, _) in enumerate(entries, start=1):
        member.position = position
    await session.commit()


async def set_supply_current_user(session: AsyncSession, supply_type: SupplyType, user_id: int) -> None:
    if not await is_supply_queue_member(session, supply_type, user_id):
        raise DomainError("Keyingi navbatchi ushbu navbatda bo‘lishi kerak.")
    if await session.get(SupplyActiveTask, supply_type):
        raise DomainError("Ochiq vazifa bor. Uni yakunlang yoki transfer qiling.")
    state = await session.get(SupplyRotationState, supply_type, with_for_update=True)
    if state is None:
        session.add(SupplyRotationState(supply_type=supply_type, current_user_id=user_id))
    else:
        state.current_user_id = user_id
    await session.commit()


async def _next_supply_user(session: AsyncSession, supply_type: SupplyType, user_id: int) -> int:
    current = await session.scalar(
        select(SupplyQueueMember).where(
            SupplyQueueMember.supply_type == supply_type, SupplyQueueMember.user_id == user_id
        )
    )
    entries = await active_supply_queue(session, supply_type)
    if not entries:
        raise DomainError("Navbat bo‘sh.")
    if current:
        for member, _ in entries:
            if member.position > current.position:
                return member.user_id
    return entries[0][0].user_id


async def _is_active_room_user(session: AsyncSession, user_id: int) -> bool:
    active_user_ids = union(
        select(FoodQueueMember.user_id).where(FoodQueueMember.is_active.is_(True)),
        select(SupplyQueueMember.user_id).where(SupplyQueueMember.is_active.is_(True)),
    )
    return bool(await session.scalar(select(User.id).where(User.id == user_id, User.id.in_(active_user_ids))))


async def active_room_users(session: AsyncSession) -> list[User]:
    active_user_ids = union(
        select(FoodQueueMember.user_id).where(FoodQueueMember.is_active.is_(True)),
        select(SupplyQueueMember.user_id).where(SupplyQueueMember.is_active.is_(True)),
    )
    return list((await session.scalars(select(User).where(User.id.in_(active_user_ids)).order_by(User.full_name))).all())


async def open_supply_task(session: AsyncSession, supply_type: SupplyType, requester_user_id: int) -> SupplyTask:
    if not await _is_active_room_user(session, requester_user_id):
        raise DomainError("Faqat faol xonadosh bu vazifani ochishi mumkin.")
    state = await session.get(SupplyRotationState, supply_type, with_for_update=True)
    if state is None:
        raise DomainError("Admin avval bu navbat uchun birinchi odamni tanlashi kerak.")
    if await session.get(SupplyActiveTask, supply_type, with_for_update=True):
        raise DomainError("Bu tur uchun allaqachon ochiq vazifa bor.")
    task = SupplyTask(
        supply_type=supply_type,
        requester_user_id=requester_user_id,
        scheduled_user_id=state.current_user_id,
        assigned_user_id=state.current_user_id,
    )
    session.add(task)
    await session.flush()
    session.add(SupplyActiveTask(supply_type=supply_type, task_id=task.id))
    await session.commit()
    return task


async def report_supply_brought(
    session: AsyncSession, task_id: int, user_id: int, now: datetime | None = None
) -> SupplyVerificationPoll:
    now = now or utc_now()
    task = await session.scalar(select(SupplyTask).where(SupplyTask.id == task_id).with_for_update())
    if task is None or task.status != SupplyTaskStatus.AWAITING_DELIVERY:
        raise DomainError("Bu vazifa hozir tasdiqlashga tayyor emas.")
    if task.assigned_user_id != user_id:
        raise DomainError("Faqat hozirgi navbatchi bu tugmani bosa oladi.")
    attempt = await session.scalar(
        select(func.max(SupplyVerificationPoll.attempt)).where(SupplyVerificationPoll.task_id == task.id)
    ) or 0
    poll = SupplyVerificationPoll(task_id=task.id, attempt=attempt + 1, closes_at=now + timedelta(minutes=30))
    task.status = SupplyTaskStatus.VERIFYING
    session.add(poll)
    await session.commit()
    return poll


async def cast_supply_vote(
    session: AsyncSession, poll_id: int, voter_user_id: int, value: VoteValue, now: datetime | None = None
) -> None:
    now = now or utc_now()
    poll = await session.get(SupplyVerificationPoll, poll_id)
    if poll is None or poll.status != SupplyPollStatus.OPEN or poll.closes_at <= now:
        raise DomainError("Bu so‘rovnoma yopilgan.")
    task = await session.get(SupplyTask, poll.task_id)
    if task is None or task.assigned_user_id == voter_user_id or not await _is_active_room_user(session, voter_user_id):
        raise DomainError("Bu so‘rovnomada ovoz bera olmaysiz.")
    vote = await session.scalar(
        select(SupplyPollVote).where(
            SupplyPollVote.poll_id == poll_id, SupplyPollVote.voter_user_id == voter_user_id
        )
    )
    if vote is None:
        session.add(SupplyPollVote(poll_id=poll_id, voter_user_id=voter_user_id, value=value))
    else:
        vote.value = value
        vote.updated_at = now
    await session.commit()


async def resolve_supply_poll(session: AsyncSession, poll: SupplyVerificationPoll, now: datetime | None = None) -> bool:
    now = now or utc_now()
    poll = await session.scalar(
        select(SupplyVerificationPoll).where(SupplyVerificationPoll.id == poll.id).with_for_update()
    )
    assert poll is not None
    if poll.status == SupplyPollStatus.CLOSED:
        task = await session.get(SupplyTask, poll.task_id)
        return bool(task and task.status == SupplyTaskStatus.COMPLETED)
    if poll.closes_at > now:
        raise DomainError("So‘rovnoma hali yopilmagan.")
    task = await session.scalar(select(SupplyTask).where(SupplyTask.id == poll.task_id).with_for_update())
    assert task is not None
    no_votes = await session.scalar(
        select(func.count(SupplyPollVote.id)).where(
            SupplyPollVote.poll_id == poll.id, SupplyPollVote.value == VoteValue.NO
        )
    ) or 0
    # A single objection keeps the task with the current assignee.
    passed = no_votes == 0
    poll.status = SupplyPollStatus.CLOSED
    if passed:
        state = await session.get(SupplyRotationState, task.supply_type, with_for_update=True)
        assert state is not None
        # A person who accepted a supply transfer has completed this turn, so the
        # next supply turn starts after the effective delivery person.
        state.current_user_id = await _next_supply_user(session, task.supply_type, task.assigned_user_id)
        task.status = SupplyTaskStatus.COMPLETED
        task.completed_at = now
        await session.execute(delete(SupplyActiveTask).where(SupplyActiveTask.supply_type == task.supply_type))
    else:
        task.status = SupplyTaskStatus.AWAITING_DELIVERY
    await session.commit()
    return passed


async def create_supply_transfer(session: AsyncSession, task_id: int, from_user_id: int, to_user_id: int) -> SupplyTransferRequest:
    task = await session.scalar(select(SupplyTask).where(SupplyTask.id == task_id).with_for_update())
    if task is None or task.status != SupplyTaskStatus.AWAITING_DELIVERY or task.assigned_user_id != from_user_id:
        raise DomainError("Bu vazifani hozir o‘tkaza olmaysiz.")
    if from_user_id == to_user_id or not await is_supply_queue_member(session, task.supply_type, to_user_id):
        raise DomainError("Shu navbatdagi boshqa faol xonadoshni tanlang.")
    pending = await session.scalar(
        select(SupplyTransferRequest.id).where(
            SupplyTransferRequest.task_id == task.id, SupplyTransferRequest.status == SupplyTransferStatus.PENDING
        )
    )
    if pending:
        raise DomainError("Bu vazifa uchun transfer javobi kutilmoqda.")
    request = SupplyTransferRequest(task_id=task.id, from_user_id=from_user_id, to_user_id=to_user_id)
    session.add(request)
    await session.commit()
    return request


async def decide_supply_transfer(
    session: AsyncSession, request_id: int, recipient_user_id: int, accepted: bool
) -> tuple[SupplyTransferRequest, SupplyTask]:
    request = await session.scalar(
        select(SupplyTransferRequest).where(SupplyTransferRequest.id == request_id).with_for_update()
    )
    if request is None or request.to_user_id != recipient_user_id or request.status != SupplyTransferStatus.PENDING:
        raise DomainError("Bu transfer so‘rovi faol emas yoki sizga tegishli emas.")
    task = await session.scalar(select(SupplyTask).where(SupplyTask.id == request.task_id).with_for_update())
    assert task is not None
    if task.status != SupplyTaskStatus.AWAITING_DELIVERY or task.assigned_user_id != request.from_user_id:
        request.status = SupplyTransferStatus.EXPIRED
        request.resolved_at = utc_now()
        await session.commit()
        raise DomainError("Bu vazifa endi transfer qilinmaydi.")
    request.status = SupplyTransferStatus.ACCEPTED if accepted else SupplyTransferStatus.REJECTED
    request.resolved_at = utc_now()
    if accepted:
        task.previous_assignee_user_id = task.assigned_user_id
        task.assigned_user_id = recipient_user_id
        task.notification_revision += 1
    await session.commit()
    return request, task


async def reassign_supply_task(session: AsyncSession, task_id: int, user_id: int) -> SupplyTask:
    """Admin override for an open supply task before verification starts."""
    task = await session.scalar(select(SupplyTask).where(SupplyTask.id == task_id).with_for_update())
    if task is None or task.status != SupplyTaskStatus.AWAITING_DELIVERY:
        raise DomainError("Faqat tekshiruv boshlanmagan faol vazifani almashtira olasiz.")
    if not await is_supply_queue_member(session, task.supply_type, user_id):
        raise DomainError("Yangi navbatchi shu navbatdagi faol xonadosh bo‘lishi kerak.")
    if task.assigned_user_id == user_id:
        return task
    task.previous_assignee_user_id = task.assigned_user_id
    task.assigned_user_id = user_id
    task.notification_revision += 1
    pending_requests = await session.scalars(
        select(SupplyTransferRequest).where(
            SupplyTransferRequest.task_id == task.id,
            SupplyTransferRequest.status == SupplyTransferStatus.PENDING,
        )
    )
    for request in pending_requests:
        request.status = SupplyTransferStatus.EXPIRED
        request.resolved_at = utc_now()
    await session.commit()
    return task


async def expire_supply_transfers(
    session: AsyncSession, now: datetime | None = None
) -> list[SupplyTransferRequest]:
    now = now or utc_now()
    requests = list(
        (
            await session.scalars(
                select(SupplyTransferRequest)
                .where(
                    SupplyTransferRequest.status == SupplyTransferStatus.PENDING,
                    SupplyTransferRequest.created_at <= now - timedelta(minutes=15),
                )
                .with_for_update()
            )
        ).all()
    )
    for request in requests:
        request.status = SupplyTransferStatus.EXPIRED
        request.resolved_at = now
    if requests:
        await session.commit()
    return requests


async def claim_supply_notification(
    session: AsyncSession,
    task_id: int,
    kind: SupplyNotificationKind,
    event_key: str,
    target_key: str,
    recipient_user_id: int | None = None,
    chat_id: int | None = None,
    poll_id: int | None = None,
) -> SupplyNotificationLog | None:
    log = await session.scalar(
        select(SupplyNotificationLog).where(
            SupplyNotificationLog.task_id == task_id,
            SupplyNotificationLog.event_key == event_key,
            SupplyNotificationLog.target_key == target_key,
        )
    )
    if log and (log.sent_at or log.is_terminal or (log.next_attempt_at and log.next_attempt_at > utc_now())):
        return None
    if log is None:
        log = SupplyNotificationLog(
            task_id=task_id,
            poll_id=poll_id,
            kind=kind,
            event_key=event_key,
            target_key=target_key,
            recipient_user_id=recipient_user_id,
            chat_id=chat_id,
        )
        session.add(log)
        await session.commit()
    return log


async def finish_supply_notification(
    session: AsyncSession, log: SupplyNotificationLog, error: str | None = None, terminal: bool = False
) -> None:
    if error:
        log.error = error
        log.attempts += 1
        log.is_terminal = terminal
        log.next_attempt_at = utc_now() + timedelta(minutes=min(5 * log.attempts, 60))
    else:
        log.sent_at = utc_now()
        log.error = None
        log.next_attempt_at = None
    await session.commit()


async def create_completion_poll(
    session: AsyncSession, assignment: FoodAssignment, timezone_name: str
) -> CompletionPoll:
    existing = await session.scalar(
        select(CompletionPoll).where(CompletionPoll.assignment_id == assignment.id)
    )
    if existing:
        return existing
    zone = ZoneInfo(timezone_name)
    closes_at = datetime.combine(assignment.duty_date, time(23, 59), zone).astimezone(UTC)
    poll = CompletionPoll(assignment_id=assignment.id, closes_at=closes_at)
    session.add(poll)
    await session.commit()
    return poll


async def confirm_food_prepared(
    session: AsyncSession,
    assignment_id: int,
    user_id: int,
    timezone_name: str,
    now: datetime | None = None,
) -> FoodAssignment:
    """Record the duty holder's self-report before the local-day deadline."""
    now = now or utc_now()
    assignment = await session.scalar(
        select(FoodAssignment).where(FoodAssignment.id == assignment_id).with_for_update()
    )
    if assignment is None or assignment.status != AssignmentStatus.ACTIVE:
        raise DomainError("Bu navbat endi faol emas.")
    if assignment.assigned_user_id != user_id:
        raise DomainError("Faqat hozirgi navbatchi bu tasdiqni bera oladi.")
    deadline = datetime.combine(assignment.duty_date + timedelta(days=1), time.min, ZoneInfo(timezone_name)).astimezone(UTC)
    if now >= deadline:
        raise DomainError("Bugungi tasdiqlash vaqti tugagan.")
    if assignment.reported_done_at is None:
        assignment.reported_done_at = now
        await session.commit()
    return assignment


async def cast_vote(
    session: AsyncSession, poll_id: int, voter_id: int, value: VoteValue, now: datetime | None = None
) -> None:
    now = now or utc_now()
    poll = await session.get(CompletionPoll, poll_id)
    if poll is None or poll.status != PollStatus.OPEN or poll.closes_at <= now:
        raise DomainError("Bu so‘rovnoma yopilgan.")
    assignment = await session.get(FoodAssignment, poll.assignment_id)
    if assignment is None or assignment.assigned_user_id == voter_id:
        raise DomainError("Bu so‘rovnomada ovoz bera olmaysiz.")
    if not await is_queue_member(session, voter_id):
        raise DomainError("Faqat faol xonadoshlar ovoz bera oladi.")
    vote = await session.scalar(
        select(PollVote).where(PollVote.poll_id == poll_id, PollVote.voter_user_id == voter_id)
    )
    if vote is None:
        session.add(PollVote(poll_id=poll_id, voter_user_id=voter_id, value=value))
    else:
        vote.value = value
        vote.updated_at = now
    await session.commit()


async def resolve_poll(session: AsyncSession, poll: CompletionPoll, now: datetime | None = None) -> FoodAssignment:
    now = now or utc_now()
    poll = await session.scalar(select(CompletionPoll).where(CompletionPoll.id == poll.id).with_for_update())
    assert poll is not None
    if poll.status == PollStatus.CLOSED:
        assignment = await session.get(FoodAssignment, poll.assignment_id)
        assert assignment is not None
        return assignment
    if poll.closes_at > now:
        raise DomainError("So‘rovnoma hali yopilmagan.")
    assignment = await session.scalar(
        select(FoodAssignment).where(FoodAssignment.id == poll.assignment_id).with_for_update()
    )
    assert assignment is not None
    no_votes = await session.scalar(
        select(func.count(PollVote.id)).where(PollVote.poll_id == poll.id, PollVote.value == VoteValue.NO)
    ) or 0
    yes_votes = await session.scalar(
        select(func.count(PollVote.id)).where(PollVote.poll_id == poll.id, PollVote.value == VoteValue.YES)
    ) or 0
    # A single objection always repeats the duty. Without objections, a positive vote or
    # the duty holder's self-report is enough to advance the rotation.
    passed = no_votes == 0 and (yes_votes > 0 or assignment.reported_done_at is not None)
    assignment.status = AssignmentStatus.COMPLETED if passed else AssignmentStatus.NOT_COMPLETED
    assignment.resolved_at = now
    poll.status = PollStatus.CLOSED

    tomorrow = assignment.duty_date + timedelta(days=1)
    next_assignment = await get_assignment_for_date(session, tomorrow)
    if next_assignment is None:
        next_user_id = await _next_queue_user(session, assignment.scheduled_user_id) if passed else assignment.scheduled_user_id
        next_assignment = FoodAssignment(
            duty_date=tomorrow, scheduled_user_id=next_user_id, assigned_user_id=next_user_id
        )
        session.add(next_assignment)
    await session.commit()
    return assignment


async def poll_has_no_activity(session: AsyncSession, poll: CompletionPoll) -> bool:
    """Return whether neither roommates nor the duty holder provided a signal."""
    assignment = await session.get(FoodAssignment, poll.assignment_id)
    if assignment is None or assignment.reported_done_at is not None:
        return False
    vote_count = await session.scalar(select(func.count(PollVote.id)).where(PollVote.poll_id == poll.id)) or 0
    return vote_count == 0


async def create_transfer_request(
    session: AsyncSession, assignment: FoodAssignment, from_user_id: int, to_user_id: int, comment: str | None
) -> TransferRequest:
    if assignment.status != AssignmentStatus.ACTIVE or assignment.assigned_user_id != from_user_id:
        raise DomainError("Bu navbatni o‘tkaza olmaysiz.")
    if await session.scalar(select(CompletionPoll.id).where(CompletionPoll.assignment_id == assignment.id)):
        raise DomainError("Kechki tekshiruv boshlanganidan keyin navbatni o‘tkazib bo‘lmaydi.")
    if from_user_id == to_user_id or not await is_queue_member(session, to_user_id):
        raise DomainError("Boshqa faol xonadoshni tanlang.")
    await session.execute(
        select(TransferRequest)
        .where(TransferRequest.assignment_id == assignment.id, TransferRequest.status == TransferStatus.PENDING)
        .with_for_update()
    )
    pending = await session.scalar(
        select(TransferRequest.id).where(
            TransferRequest.assignment_id == assignment.id, TransferRequest.status == TransferStatus.PENDING
        )
    )
    if pending:
        raise DomainError("Bu navbat uchun javobi kutilayotgan so‘rov bor.")
    request = TransferRequest(
        assignment_id=assignment.id, from_user_id=from_user_id, to_user_id=to_user_id, comment=comment or None
    )
    session.add(request)
    await session.commit()
    return request


async def decide_transfer(
    session: AsyncSession, request_id: int, recipient_user_id: int, accepted: bool
) -> tuple[TransferRequest, FoodAssignment]:
    request = await session.scalar(select(TransferRequest).where(TransferRequest.id == request_id).with_for_update())
    if request is None or request.to_user_id != recipient_user_id:
        raise DomainError("Bu so‘rov sizga tegishli emas.")
    if request.status != TransferStatus.PENDING:
        raise DomainError("Bu transfer so‘rovi avval yakunlangan.")
    assignment = await session.get(FoodAssignment, request.assignment_id, with_for_update=True)
    assert assignment is not None
    if assignment.status != AssignmentStatus.ACTIVE or assignment.assigned_user_id != request.from_user_id:
        request.status = TransferStatus.EXPIRED
        request.resolved_at = utc_now()
        await session.commit()
        raise DomainError("Bu navbat endi faol emas.")
    request.status = TransferStatus.ACCEPTED if accepted else TransferStatus.REJECTED
    request.resolved_at = utc_now()
    if accepted:
        assignment.assigned_user_id = recipient_user_id
        assignment.reported_done_at = None
        assignment.notification_revision += 1
        await enqueue_group_notification(session, assignment, GroupNotificationKind.DUTY_CHANGED)
    await session.commit()
    return request, assignment


async def reassign_today(session: AsyncSession, assignment: FoodAssignment, user_id: int) -> None:
    if assignment.status != AssignmentStatus.ACTIVE or not await is_queue_member(session, user_id):
        raise DomainError("Faol navbatdagi foydalanuvchini tanlang.")
    if await session.scalar(select(CompletionPoll.id).where(CompletionPoll.assignment_id == assignment.id)):
        raise DomainError("Kechki tekshiruv boshlanganidan keyin navbatchini almashtirib bo‘lmaydi.")
    if assignment.assigned_user_id != user_id:
        assignment.assigned_user_id = user_id
        assignment.reported_done_at = None
        assignment.notification_revision += 1
        await enqueue_group_notification(session, assignment, GroupNotificationKind.DUTY_CHANGED)
    pending_requests = await session.scalars(
        select(TransferRequest).where(
            TransferRequest.assignment_id == assignment.id, TransferRequest.status == TransferStatus.PENDING
        )
    )
    for request in pending_requests:
        request.status = TransferStatus.EXPIRED
        request.resolved_at = utc_now()
    await session.commit()


async def claim_notification(
    session: AsyncSession, assignment_id: int, kind: NotificationKind, recipient_user_id: int
) -> bool:
    log = await session.scalar(
        select(NotificationLog).where(
            NotificationLog.assignment_id == assignment_id,
            NotificationLog.kind == kind,
            NotificationLog.recipient_user_id == recipient_user_id,
        )
    )
    if log and log.sent_at:
        return False
    if log and (log.is_terminal or (log.next_attempt_at and log.next_attempt_at > utc_now())):
        return False
    if log is None:
        session.add(NotificationLog(assignment_id=assignment_id, kind=kind, recipient_user_id=recipient_user_id))
        await session.commit()
    return True


async def finish_notification(
    session: AsyncSession,
    assignment_id: int,
    kind: NotificationKind,
    recipient_user_id: int,
    error: str | None = None,
    terminal: bool = False,
) -> None:
    log = await session.scalar(
        select(NotificationLog).where(
            NotificationLog.assignment_id == assignment_id,
            NotificationLog.kind == kind,
            NotificationLog.recipient_user_id == recipient_user_id,
        )
    )
    if log is None:
        return
    log.sent_at = None if error else utc_now()
    log.error = error
    if error:
        log.attempts += 1
        log.is_terminal = terminal
        delay_minutes = min(5 * log.attempts, 60)
        log.next_attempt_at = utc_now() + timedelta(minutes=delay_minutes)
    else:
        log.next_attempt_at = None
    await session.commit()


async def pending_group_notifications(session: AsyncSession, now: datetime) -> list[GroupNotificationLog]:
    room = await active_room(session)
    if room is None:
        return []
    result = await session.scalars(
        select(GroupNotificationLog)
        .where(
            GroupNotificationLog.chat_id == room.chat_id,
            GroupNotificationLog.sent_at.is_(None),
            GroupNotificationLog.is_terminal.is_(False),
        )
        .order_by(GroupNotificationLog.id)
    )
    return [
        log
        for log in result
        if log.next_attempt_at is None or log.next_attempt_at <= now
    ]


async def finish_group_notification(
    session: AsyncSession,
    log: GroupNotificationLog,
    error: str | None = None,
    terminal: bool = False,
) -> None:
    if error:
        log.error = error
        log.attempts += 1
        log.is_terminal = terminal
        delay_minutes = min(5 * log.attempts, 60)
        log.next_attempt_at = utc_now() + timedelta(minutes=delay_minutes)
    else:
        log.sent_at = utc_now()
        log.error = None
        log.next_attempt_at = None
    await session.commit()
