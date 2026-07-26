import os
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")

from app.database import Base
from app.models import AssignmentStatus, GroupNotificationKind, GroupNotificationLog, User, VoteValue
from app.services import (
    add_queue_member,
    cast_vote,
    confirm_food_prepared,
    create_completion_poll,
    create_initial_assignment,
    enqueue_group_notification,
    get_assignment_for_date,
    reassign_today,
    resolve_poll,
    set_active_room,
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
