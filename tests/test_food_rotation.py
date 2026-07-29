import os
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")

import app.scheduler as scheduler_module
from app.config import Settings
from app.database import Base
from app.models import (
    AssignmentStatus,
    GroupNotificationKind,
    GroupNotificationLog,
    ManualReminderChannel,
    ManualReminderLog,
    SupplyRotationState,
    SupplyTaskStatus,
    SupplyType,
    User,
    VoteValue,
)
from app.scheduler import DutyScheduler
from app.services import (
    active_manual_reminder_targets,
    add_queue_member,
    add_supply_queue_member,
    cast_supply_vote,
    cast_vote,
    confirm_food_prepared,
    create_completion_poll,
    create_initial_assignment,
    create_supply_transfer,
    decide_supply_transfer,
    enqueue_group_notification,
    food_history_page,
    get_assignment_for_date,
    get_or_create_user,
    open_supply_task,
    queue_manual_reminders,
    reassign_today,
    report_supply_brought,
    resolve_poll,
    resolve_supply_poll,
    set_active_room,
    set_supply_current_user,
    supply_history_page,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as current_session:
        yield current_session
    await engine.dispose()


async def add_people(session, amount: int) -> list[User]:
    people = [User(telegram_id=10_000 + index, full_name=f"User {index}") for index in range(amount)]
    session.add_all(people)
    await session.commit()
    for person in people:
        await add_queue_member(session, person.id)
    return people


@pytest.mark.asyncio
async def test_a_yes_vote_advances_to_the_next_scheduled_person(session):
    first, second = await add_people(session, 2)
    assignment = await create_initial_assignment(session, date(2026, 1, 1), first.id)
    poll = await create_completion_poll(session, assignment, "Asia/Tashkent")
    await cast_vote(session, poll.id, second.id, VoteValue.YES, now=poll.closes_at - timedelta(seconds=1))

    await resolve_poll(session, poll, now=poll.closes_at + timedelta(seconds=1))
    tomorrow = await get_assignment_for_date(session, date(2026, 1, 2))

    assert assignment.status == AssignmentStatus.COMPLETED
    assert tomorrow is not None
    assert tomorrow.scheduled_user_id == second.id
    assert tomorrow.assigned_user_id == second.id


@pytest.mark.asyncio
async def test_two_no_votes_keep_the_same_person_on_duty(session):
    first, second, third = await add_people(session, 3)
    assignment = await create_initial_assignment(session, date(2026, 1, 1), first.id)
    poll = await create_completion_poll(session, assignment, "Asia/Tashkent")
    before_close = poll.closes_at - timedelta(seconds=1)
    await cast_vote(session, poll.id, second.id, VoteValue.NO, now=before_close)
    await cast_vote(session, poll.id, third.id, VoteValue.NO, now=before_close)

    await resolve_poll(session, poll, now=poll.closes_at + timedelta(seconds=1))
    tomorrow = await get_assignment_for_date(session, date(2026, 1, 2))

    assert assignment.status == AssignmentStatus.NOT_COMPLETED
    assert tomorrow is not None
    assert tomorrow.scheduled_user_id == first.id


@pytest.mark.asyncio
async def test_self_report_without_votes_advances_to_the_next_scheduled_person(session):
    first, second = await add_people(session, 2)
    assignment = await create_initial_assignment(session, date(2026, 1, 1), first.id)
    poll = await create_completion_poll(session, assignment, "Asia/Tashkent")
    await confirm_food_prepared(
        session,
        assignment.id,
        first.id,
        "Asia/Tashkent",
        now=poll.closes_at - timedelta(seconds=1),
    )

    await resolve_poll(session, poll, now=poll.closes_at + timedelta(seconds=1))
    tomorrow = await get_assignment_for_date(session, date(2026, 1, 2))

    assert assignment.status == AssignmentStatus.COMPLETED
    assert tomorrow is not None
    assert tomorrow.scheduled_user_id == second.id


@pytest.mark.asyncio
async def test_no_votes_and_no_self_report_repeats_the_duty(session):
    first, second = await add_people(session, 2)
    assignment = await create_initial_assignment(session, date(2026, 1, 1), first.id)
    poll = await create_completion_poll(session, assignment, "Asia/Tashkent")

    await resolve_poll(session, poll, now=poll.closes_at + timedelta(seconds=1))
    tomorrow = await get_assignment_for_date(session, date(2026, 1, 2))

    assert assignment.status == AssignmentStatus.NOT_COMPLETED
    assert tomorrow is not None
    assert tomorrow.scheduled_user_id == first.id


@pytest.mark.asyncio
async def test_group_notifications_are_idempotent_and_follow_reassignment(session):
    first, second = await add_people(session, 2)
    assignment = await create_initial_assignment(session, date(2026, 1, 1), first.id)
    await set_active_room(session, -1001234567890, "Xonadoshlar", first.id)

    first_log = await enqueue_group_notification(session, assignment, GroupNotificationKind.DAILY_DUTY)
    await session.commit()
    duplicate_log = await enqueue_group_notification(session, assignment, GroupNotificationKind.DAILY_DUTY)
    await session.commit()
    assert first_log is not None
    assert duplicate_log is not None
    assert duplicate_log.id == first_log.id

    await reassign_today(session, assignment, second.id)
    logs = list((await session.scalars(select(GroupNotificationLog).order_by(GroupNotificationLog.id))).all())

    assert [(log.kind, log.target_user_id, log.revision) for log in logs] == [
        (GroupNotificationKind.DAILY_DUTY, first.id, 0),
        (GroupNotificationKind.DUTY_CHANGED, second.id, 1),
    ]


@pytest.mark.asyncio
async def test_supply_task_advances_only_after_delivery_verification(session):
    first, second, third = await add_people(session, 3)
    for person in (first, second, third):
        await add_supply_queue_member(session, SupplyType.BREAD, person.id)
    await set_supply_current_user(session, SupplyType.BREAD, first.id)

    task = await open_supply_task(session, SupplyType.BREAD, second.id)
    poll = await report_supply_brought(session, task.id, first.id)
    await cast_supply_vote(session, poll.id, second.id, VoteValue.YES, now=poll.closes_at - timedelta(seconds=1))
    assert await resolve_supply_poll(session, poll, now=poll.closes_at + timedelta(seconds=1)) is True

    state = await session.get(SupplyRotationState, SupplyType.BREAD)
    assert task.status == SupplyTaskStatus.COMPLETED
    assert state is not None
    assert state.current_user_id == second.id


@pytest.mark.asyncio
async def test_two_supply_no_votes_keep_the_same_task_open(session):
    first, second, third = await add_people(session, 3)
    for person in (first, second, third):
        await add_supply_queue_member(session, SupplyType.WATER, person.id)
    await set_supply_current_user(session, SupplyType.WATER, first.id)

    task = await open_supply_task(session, SupplyType.WATER, second.id)
    poll = await report_supply_brought(session, task.id, first.id)
    before_close = poll.closes_at - timedelta(seconds=1)
    await cast_supply_vote(session, poll.id, second.id, VoteValue.NO, now=before_close)
    await cast_supply_vote(session, poll.id, third.id, VoteValue.NO, now=before_close)
    assert await resolve_supply_poll(session, poll, now=poll.closes_at + timedelta(seconds=1)) is False

    state = await session.get(SupplyRotationState, SupplyType.WATER)
    assert task.status == SupplyTaskStatus.AWAITING_DELIVERY
    assert state is not None
    assert state.current_user_id == first.id


@pytest.mark.asyncio
async def test_supply_transfer_advances_after_the_effective_delivery_person(session):
    first, second, third = await add_people(session, 3)
    for person in (first, second, third):
        await add_supply_queue_member(session, SupplyType.BREAD, person.id)
    await set_supply_current_user(session, SupplyType.BREAD, first.id)
    task = await open_supply_task(session, SupplyType.BREAD, third.id)

    request = await create_supply_transfer(session, task.id, first.id, second.id)
    _, task = await decide_supply_transfer(session, request.id, second.id, accepted=True)
    assert task.scheduled_user_id == first.id
    assert task.assigned_user_id == second.id

    poll = await report_supply_brought(session, task.id, second.id)
    await cast_supply_vote(session, poll.id, third.id, VoteValue.YES, now=poll.closes_at - timedelta(seconds=1))
    assert await resolve_supply_poll(session, poll, now=poll.closes_at + timedelta(seconds=1)) is True

    state = await session.get(SupplyRotationState, SupplyType.BREAD)
    assert state is not None
    assert state.current_user_id == third.id


@pytest.mark.asyncio
async def test_history_returns_effective_assignee_after_a_food_transfer(session):
    first, second, third = await add_people(session, 3)
    assignment = await create_initial_assignment(session, date(2026, 1, 1), first.id)
    await reassign_today(session, assignment, second.id)
    poll = await create_completion_poll(session, assignment, "Asia/Tashkent")
    await cast_vote(
        session, poll.id, third.id, VoteValue.YES, now=poll.closes_at - timedelta(seconds=1)
    )
    await resolve_poll(session, poll, now=poll.closes_at + timedelta(seconds=1))

    items, has_next = await food_history_page(session)

    assert has_next is False
    assert [(item.id, scheduled.id, assigned.id) for item, scheduled, assigned in items] == [
        (assignment.id, first.id, second.id)
    ]


@pytest.mark.asyncio
async def test_supply_history_returns_completed_transferred_task(session):
    first, second, third = await add_people(session, 3)
    for person in (first, second, third):
        await add_supply_queue_member(session, SupplyType.WATER, person.id)
    await set_supply_current_user(session, SupplyType.WATER, first.id)
    task = await open_supply_task(session, SupplyType.WATER, third.id)
    request = await create_supply_transfer(session, task.id, first.id, second.id)
    await decide_supply_transfer(session, request.id, second.id, accepted=True)
    poll = await report_supply_brought(session, task.id, second.id)
    await cast_supply_vote(
        session, poll.id, third.id, VoteValue.YES, now=poll.closes_at - timedelta(seconds=1)
    )
    await resolve_supply_poll(session, poll, now=poll.closes_at + timedelta(seconds=1))

    items, has_next = await supply_history_page(session, SupplyType.WATER)

    assert has_next is False
    history = [
        (item.id, requester.id, scheduled.id, assigned.id)
        for item, requester, scheduled, assigned in items
    ]
    assert history == [(task.id, third.id, first.id, second.id)]


@pytest.mark.asyncio
async def test_admin_role_is_revoked_when_id_is_removed_from_configuration(session):
    user = await get_or_create_user(session, 9988, "Admin", None, is_admin=True)
    assert user.is_admin is True

    user = await get_or_create_user(session, 9988, "Admin", None, is_admin=False)
    assert user.is_admin is False


@pytest.mark.asyncio
async def test_manual_reminder_queues_direct_and_group_delivery_with_cooldown(session):
    admin, food_assignee, supply_assignee = await add_people(session, 3)
    assignment = await create_initial_assignment(session, date(2026, 1, 1), food_assignee.id)
    for person in (admin, food_assignee, supply_assignee):
        await add_supply_queue_member(session, SupplyType.BREAD, person.id)
    await set_supply_current_user(session, SupplyType.BREAD, supply_assignee.id)
    task = await open_supply_task(session, SupplyType.BREAD, admin.id)
    await set_active_room(session, -1001234567890, "Xonadoshlar", admin.id)
    now = datetime(2026, 1, 1, 10, tzinfo=UTC)

    targets = await active_manual_reminder_targets(session, assignment.duty_date, now)
    references = [(target.kind, target.reference_id) for target in targets]
    result = await queue_manual_reminders(session, admin.id, assignment.duty_date, references, now)
    logs = list((await session.scalars(select(ManualReminderLog).order_by(ManualReminderLog.id))).all())

    assert {(target.kind, target.reference_id) for target in targets} == {
        ("food", assignment.id),
        ("supply", task.id),
    }
    assert result.target_count == 2
    assert result.skipped_cooldown_count == 0
    assert len(logs) == 4
    assert {log.channel for log in logs} == {ManualReminderChannel.DIRECT, ManualReminderChannel.GROUP}
    assert {log.chat_id for log in logs if log.channel == ManualReminderChannel.GROUP} == {-1001234567890}

    repeated = await queue_manual_reminders(
        session,
        admin.id,
        assignment.duty_date,
        [("food", assignment.id)],
        now + timedelta(minutes=10),
    )

    assert repeated.target_count == 0
    assert repeated.skipped_cooldown_count == 1


@pytest.mark.asyncio
async def test_manual_reminder_dispatch_sends_to_private_chat_and_group(session, monkeypatch):
    admin, assignee = await add_people(session, 2)
    assignment = await create_initial_assignment(session, date(2026, 1, 1), assignee.id)
    await set_active_room(session, -1001234567890, "Xonadoshlar", admin.id)
    result = await queue_manual_reminders(
        session,
        admin.id,
        assignment.duty_date,
        [("food", assignment.id)],
        datetime(2026, 1, 1, 10, tzinfo=UTC),
    )

    class RecordingBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str]] = []

        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            self.messages.append((chat_id, text))

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(scheduler_module, "SessionFactory", factory)
    bot = RecordingBot()
    scheduler = DutyScheduler(
        bot,
        Settings(bot_token="test-token", database_url="sqlite+aiosqlite://", timezone="Asia/Tashkent"),
    )

    delivered, failed = await scheduler.dispatch_manual_reminders(result.log_ids)

    assert (delivered, failed) == (2, 0)
    assert {chat_id for chat_id, _ in bot.messages} == {assignee.telegram_id, -1001234567890}
    assert any("Sizda bugungi ovqat navbati bor" in text for _, text in bot.messages)
    assert any("bugungi ovqat navbati sizda" in text for _, text in bot.messages)
