import enum
from datetime import UTC, date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class AssignmentStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    NOT_COMPLETED = "not_completed"


class PollStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class VoteValue(str, enum.Enum):
    YES = "yes"
    NO = "no"


class TransferStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class NotificationKind(str, enum.Enum):
    MORNING = "morning"
    NOON = "noon"
    EVENING = "evening"
    POLL = "poll"
    ADMIN_ALERT = "admin_alert"
    GROUP_ERROR = "group_error"


class GroupNotificationKind(str, enum.Enum):
    DAILY_DUTY = "daily_duty"
    DUTY_CHANGED = "duty_changed"


class SupplyType(str, enum.Enum):
    BREAD = "bread"
    WATER = "water"


class SupplyTaskStatus(str, enum.Enum):
    AWAITING_DELIVERY = "awaiting_delivery"
    VERIFYING = "verifying"
    COMPLETED = "completed"


class SupplyPollStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class SupplyTransferStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class SupplyNotificationKind(str, enum.Enum):
    ASSIGNMENT_DIRECT = "assignment_direct"
    ASSIGNMENT_GROUP = "assignment_group"
    POLL = "poll"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FoodQueueMember(Base):
    __tablename__ = "food_queue_members"
    __table_args__ = (UniqueConstraint("user_id", name="uq_food_queue_member_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    position: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class FoodAssignment(Base):
    __tablename__ = "food_assignments"
    __table_args__ = (UniqueConstraint("duty_date", name="uq_food_assignment_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    duty_date: Mapped[date] = mapped_column(Date, index=True)
    # The rotation is advanced from this user, even if today's task is transferred.
    scheduled_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    assigned_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(AssignmentStatus), default=AssignmentStatus.ACTIVE, nullable=False
    )
    # Supporting evidence only; the nightly poll still decides the rotation.
    reported_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Increments whenever today's effective duty holder changes.
    notification_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CompletionPoll(Base):
    __tablename__ = "completion_polls"
    __table_args__ = (UniqueConstraint("assignment_id", name="uq_completion_poll_assignment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("food_assignments.id", ondelete="CASCADE"))
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[PollStatus] = mapped_column(Enum(PollStatus), default=PollStatus.OPEN)


class PollVote(Base):
    __tablename__ = "poll_votes"
    __table_args__ = (UniqueConstraint("poll_id", "voter_user_id", name="uq_poll_vote_voter"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("completion_polls.id", ondelete="CASCADE"))
    voter_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    value: Mapped[VoteValue] = mapped_column(Enum(VoteValue))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TransferRequest(Base):
    __tablename__ = "transfer_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("food_assignments.id", ondelete="CASCADE"))
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TransferStatus] = mapped_column(
        Enum(TransferStatus), default=TransferStatus.PENDING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "kind", "recipient_user_id", name="uq_notification_assignment_kind_recipient"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("food_assignments.id", ondelete="CASCADE"))
    kind: Mapped[NotificationKind] = mapped_column(Enum(NotificationKind))
    recipient_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_terminal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RoomSetting(Base):
    __tablename__ = "room_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    configured_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GroupNotificationLog(Base):
    __tablename__ = "group_notification_logs"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "chat_id",
            "kind",
            "revision",
            name="uq_group_notification_assignment_chat_kind_revision",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("food_assignments.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    kind: Mapped[GroupNotificationKind] = mapped_column(Enum(GroupNotificationKind))
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_terminal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class SupplyQueueMember(Base):
    __tablename__ = "supply_queue_members"
    __table_args__ = (
        UniqueConstraint("supply_type", "user_id", name="uq_supply_queue_member"),
        UniqueConstraint("supply_type", "position", name="uq_supply_queue_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supply_type: Mapped[SupplyType] = mapped_column(Enum(SupplyType), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    position: Mapped[int] = mapped_column(Integer, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SupplyRotationState(Base):
    __tablename__ = "supply_rotation_states"

    supply_type: Mapped[SupplyType] = mapped_column(Enum(SupplyType), primary_key=True)
    current_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SupplyTask(Base):
    __tablename__ = "supply_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    supply_type: Mapped[SupplyType] = mapped_column(Enum(SupplyType), index=True)
    requester_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    scheduled_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    assigned_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    previous_assignee_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    notification_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[SupplyTaskStatus] = mapped_column(
        Enum(SupplyTaskStatus), default=SupplyTaskStatus.AWAITING_DELIVERY, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupplyActiveTask(Base):
    """One active task per supply type; this is also the concurrency guard."""

    __tablename__ = "supply_active_tasks"

    supply_type: Mapped[SupplyType] = mapped_column(Enum(SupplyType), primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("supply_tasks.id", ondelete="CASCADE"), unique=True)


class SupplyVerificationPoll(Base):
    __tablename__ = "supply_verification_polls"
    __table_args__ = (UniqueConstraint("task_id", "attempt", name="uq_supply_poll_task_attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("supply_tasks.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[SupplyPollStatus] = mapped_column(Enum(SupplyPollStatus), default=SupplyPollStatus.OPEN)


class SupplyPollVote(Base):
    __tablename__ = "supply_poll_votes"
    __table_args__ = (UniqueConstraint("poll_id", "voter_user_id", name="uq_supply_poll_vote"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("supply_verification_polls.id", ondelete="CASCADE"))
    voter_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    value: Mapped[VoteValue] = mapped_column(Enum(VoteValue))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SupplyTransferRequest(Base):
    __tablename__ = "supply_transfer_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("supply_tasks.id", ondelete="CASCADE"), index=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[SupplyTransferStatus] = mapped_column(
        Enum(SupplyTransferStatus), default=SupplyTransferStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupplyNotificationLog(Base):
    __tablename__ = "supply_notification_logs"
    __table_args__ = (UniqueConstraint("task_id", "event_key", "target_key", name="uq_supply_notification"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("supply_tasks.id", ondelete="CASCADE"), index=True)
    poll_id: Mapped[int | None] = mapped_column(
        ForeignKey("supply_verification_polls.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[SupplyNotificationKind] = mapped_column(Enum(SupplyNotificationKind))
    event_key: Mapped[str] = mapped_column(String(80))
    target_key: Mapped[str] = mapped_column(String(80))
    recipient_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_terminal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
