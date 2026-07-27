from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.config import Settings
from app.database import SessionFactory
from app.keyboards import ADMIN, BREAD_EMPTY, HISTORY, MY_DUTY, STATUS, TODAY, TRANSFER, WATER_EMPTY, main_menu
from app.models import (
    FoodAssignment,
    SupplyActiveTask,
    SupplyPollStatus,
    SupplyRotationState,
    SupplyTask,
    SupplyTaskStatus,
    SupplyTransferStatus,
    SupplyType,
    SupplyVerificationPoll,
    TransferStatus,
    User,
    VoteValue,
)
from app.services import (
    DomainError,
    active_queue,
    active_supply_queue,
    add_queue_member,
    cast_vote,
    cast_supply_vote,
    confirm_food_prepared,
    create_initial_assignment,
    create_transfer_request,
    create_supply_transfer,
    current_assignment,
    deactivate_room,
    decide_transfer,
    decide_supply_transfer,
    get_assignment_for_date,
    get_or_create_user,
    get_user_by_telegram_id,
    is_queue_member,
    move_queue_member,
    move_supply_queue_member,
    open_supply_task,
    reassign_today,
    reassign_supply_task,
    remove_queue_member,
    add_supply_queue_member,
    remove_supply_queue_member,
    report_supply_brought,
    set_supply_current_user,
    set_active_room,
)
from app.states import TransferStates


def build_router(settings: Settings) -> Router:
    router = Router(name="food-duty")
    zone = ZoneInfo(settings.timezone)

    def today_local() -> date:
        return datetime.now(zone).date()

    async def register(message: Message) -> User:
        telegram_user = message.from_user
        assert telegram_user is not None
        async with SessionFactory() as session:
            return await get_or_create_user(
                session,
                telegram_id=telegram_user.id,
                full_name=telegram_user.full_name,
                username=telegram_user.username,
                is_admin=telegram_user.id in settings.parsed_admin_ids,
            )

    async def callback_user(callback: CallbackQuery) -> User:
        assert callback.from_user is not None
        async with SessionFactory() as session:
            user = await get_user_by_telegram_id(session, callback.from_user.id)
            if user is None:
                raise DomainError("Avval /start buyrug‘ini yuboring.")
            return user

    async def require_admin(callback: CallbackQuery) -> User:
        user = await callback_user(callback)
        if not user.is_admin:
            raise DomainError("Bu bo‘lim faqat admin uchun.")
        return user

    async def user_for_message(message: Message) -> User | None:
        if message.from_user is None:
            return None
        async with SessionFactory() as session:
            return await get_user_by_telegram_id(session, message.from_user.id)

    async def show_admin(callback: CallbackQuery) -> None:
        await require_admin(callback)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="👥 Qatnashchilar", callback_data="adm:members")],
                [InlineKeyboardButton(text="↕️ Navbat tartibi", callback_data="adm:queue")],
                [InlineKeyboardButton(text="📦 Non va suv navbati", callback_data="supadm:menu")],
                [InlineKeyboardButton(text="▶️ Navbatni boshlash", callback_data="adm:start")],
                [InlineKeyboardButton(text="📍 Bugungi navbatchini almashtirish", callback_data="adm:today")],
            ]
        )
        await callback.message.answer("Admin boshqaruvi:", reply_markup=keyboard)

    async def current_text(session, today: date) -> str:
        assignment = await current_assignment(session, today)
        if assignment is None:
            return "Bugungi ovqat navbati hali boshlanmagan."
        user = await session.get(User, assignment.assigned_user_id)
        return f"🍽 Bugun ovqat navbatchisi: {user.full_name if user else 'noma’lum'}"

    def supply_label(supply_type: SupplyType) -> str:
        return "🥖 Non" if supply_type == SupplyType.BREAD else "💧 Suv"

    async def supply_status_text(session, supply_type: SupplyType) -> str:
        label = supply_label(supply_type)
        active = await session.get(SupplyActiveTask, supply_type)
        if active:
            task = await session.get(SupplyTask, active.task_id)
            if task:
                person = await session.get(User, task.assigned_user_id)
                name = person.full_name if person else "noma’lum"
                if task.status == SupplyTaskStatus.AWAITING_DELIVERY:
                    return f"{label}: {name} olib kelishi kutilmoqda."
                poll = await session.scalar(
                    select(SupplyVerificationPoll).where(
                        SupplyVerificationPoll.task_id == task.id,
                        SupplyVerificationPoll.status == SupplyPollStatus.OPEN,
                    )
                )
                if poll:
                    closes_at = poll.closes_at.astimezone(zone).strftime("%H:%M")
                    return f"{label}: {name} olib keldi, tasdiqlash {closes_at} gacha."
                return f"{label}: {name} vazifasi tekshirilmoqda."
        state = await session.get(SupplyRotationState, supply_type)
        if state is None:
            return f"{label}: navbat hali sozlanmagan."
        person = await session.get(User, state.current_user_id)
        return f"{label}: ochiq vazifa yo‘q. Keyingi navbatchi — {person.full_name if person else 'noma’lum'}."

    async def my_supply_status_text(session, supply_type: SupplyType, user_id: int) -> str:
        label = supply_label(supply_type)
        active = await session.get(SupplyActiveTask, supply_type)
        if active:
            task = await session.get(SupplyTask, active.task_id)
            if task and task.assigned_user_id == user_id:
                if task.status == SupplyTaskStatus.AWAITING_DELIVERY:
                    return f"{label}: siz olib kelishingiz kerak."
                return f"{label}: siz olib keldingiz, xonadoshlar tasdiqlashi kutilmoqda."
            if task:
                person = await session.get(User, task.assigned_user_id)
                return f"{label}: hozir {person.full_name if person else 'boshqa odam'} olib kelmoqda."
        state = await session.get(SupplyRotationState, supply_type)
        entries = await active_supply_queue(session, supply_type)
        if state is None or not entries:
            return f"{label}: navbat hali sozlanmagan."
        current_index = next((i for i, (_, person) in enumerate(entries) if person.id == state.current_user_id), None)
        user_index = next((i for i, (_, person) in enumerate(entries) if person.id == user_id), None)
        if current_index is None or user_index is None:
            return f"{label}: siz bu navbatda emassiz."
        place = (user_index - current_index) % len(entries) + 1
        return f"{label}: siz navbatda {place}-o‘rindasiz." if place > 1 else f"{label}: keyingi navbatchi sizsiz."

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        user = await register(message)
        text = "Xush kelibsiz. Bot ovqat navbatini eslatadi va tekshiradi."
        if user.is_admin:
            text += "\nSiz adminsiz: avval qatnashchilarni ovqat navbatiga qo‘shing."
        else:
            text += "\nAdmin sizni navbatga qo‘shgach, vazifalaringiz shu yerda ko‘rinadi."
        await message.answer(text, reply_markup=main_menu(user.is_admin))

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        user = await register(message)
        await message.answer("Asosiy menyu", reply_markup=main_menu(user.is_admin))

    @router.message(Command("setup_group"))
    async def setup_group(message: Message) -> None:
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.answer("Bu buyruqni xonadoshlar guruhida yuboring.")
            return
        user = await register(message)
        if not user.is_admin:
            await message.answer("Guruhni faqat admin ulay oladi.")
            return
        async with SessionFactory() as session:
            await set_active_room(session, message.chat.id, message.chat.title, user.id)
        await message.answer(
            "✅ Guruh ulandi. Har kuni 07:00 dan keyin bugungi navbatchi shu guruhda belgilanadi."
        )

    @router.message(Command("unlink_group"))
    async def unlink_group(message: Message) -> None:
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.answer("Bu buyruqni ulangan guruhda yuboring.")
            return
        user = await register(message)
        if not user.is_admin:
            await message.answer("Guruhni faqat admin uza oladi.")
            return
        async with SessionFactory() as session:
            removed = await deactivate_room(session, message.chat.id)
        await message.answer("✅ Guruh uzildi." if removed else "Bu guruh hozir ulanmagan.")

    @router.message(F.text == TODAY)
    async def today(message: Message) -> None:
        user = await user_for_message(message)
        if user is None:
            await message.answer("Avval /start buyrug‘ini yuboring.")
            return
        async with SessionFactory() as session:
            await message.answer(await current_text(session, today_local()))

    @router.message(F.text == STATUS)
    async def room_status(message: Message) -> None:
        user = await user_for_message(message)
        if user is None:
            await message.answer("Avval /start buyrug‘ini yuboring.")
            return
        async with SessionFactory() as session:
            food = await current_text(session, today_local())
            bread = await supply_status_text(session, SupplyType.BREAD)
            water = await supply_status_text(session, SupplyType.WATER)
        await message.answer(f"📋 Xona navbatlari\n\n{food}\n\n{bread}\n{water}")

    @router.message(F.text == MY_DUTY)
    async def my_duty(message: Message) -> None:
        user = await user_for_message(message)
        if user is None:
            await message.answer("Avval /start buyrug‘ini yuboring.")
            return
        async with SessionFactory() as session:
            assignment = await current_assignment(session, today_local())
            lines: list[str] = []
            buttons: list[list[InlineKeyboardButton]] = []
            if assignment and assignment.assigned_user_id == user.id:
                status = "\n✅ Tayyorlaganingiz qayd etilgan." if assignment.reported_done_at else ""
                lines.append(f"🍽 Ovqat: bugun siz navbatchisiz.{status}")
                buttons.append([InlineKeyboardButton(text="✅ Ovqat tayyorladim", callback_data=f"duty:done:{assignment.id}")])
            else:
                lines.append("🍽 Ovqat: bugun sizning navbatingiz emas.")
            for supply_type in (SupplyType.BREAD, SupplyType.WATER):
                lines.append(await my_supply_status_text(session, supply_type, user.id))
                active = await session.get(SupplyActiveTask, supply_type)
                if active:
                    task = await session.get(SupplyTask, active.task_id)
                    if task and task.assigned_user_id == user.id and task.status == SupplyTaskStatus.AWAITING_DELIVERY:
                        buttons.append(
                            [
                                InlineKeyboardButton(text="✅ Olib keldim", callback_data=f"supply:done:{task.id}"),
                                InlineKeyboardButton(text="🔄 O‘tkazish", callback_data=f"supply:transfer:{task.id}"),
                            ]
                        )
            await message.answer(
                "📌 Mening navbatlarim\n\n" + "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
            )

    @router.callback_query(F.data.startswith("duty:done:"))
    async def duty_done(callback: CallbackQuery) -> None:
        try:
            user = await callback_user(callback)
            assignment_id = int(callback.data.rsplit(":", 1)[1])
            async with SessionFactory() as session:
                await confirm_food_prepared(session, assignment_id, user.id, settings.timezone)
            await callback.answer("✅ Tayyorlaganingiz qayd etildi.")
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.message(F.text.in_({BREAD_EMPTY, WATER_EMPTY}))
    async def supply_empty(message: Message) -> None:
        user = await user_for_message(message)
        if user is None:
            await message.answer("Avval /start buyrug‘ini yuboring.")
            return
        supply_type = SupplyType.BREAD if message.text == BREAD_EMPTY else SupplyType.WATER
        label = "Non" if supply_type == SupplyType.BREAD else "Suv"
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Ha, {label.lower()} tugadi", callback_data=f"supply:open:{supply_type.value}")],
                [InlineKeyboardButton(text="Bekor qilish", callback_data="ignore")],
            ]
        )
        await message.answer(f"{label} tugaganiga ishonchingiz komilmi?", reply_markup=markup)

    @router.callback_query(F.data.startswith("supply:open:"))
    async def supply_open(callback: CallbackQuery) -> None:
        try:
            user = await callback_user(callback)
            supply_type = SupplyType(callback.data.rsplit(":", 1)[1])
            async with SessionFactory() as session:
                task = await open_supply_task(session, supply_type, user.id)
                assignee = await session.get(User, task.assigned_user_id)
            label = "Non" if supply_type == SupplyType.BREAD else "Suv"
            await callback.message.answer(
                f"✅ {label} vazifasi ochildi. Navbatchi: {assignee.full_name if assignee else 'noma’lum'}"
            )
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supply:done:"))
    async def supply_done(callback: CallbackQuery) -> None:
        try:
            user = await callback_user(callback)
            task_id = int(callback.data.rsplit(":", 1)[1])
            async with SessionFactory() as session:
                await report_supply_brought(session, task_id, user.id)
            await callback.answer("✅ Qayd etildi. Xonadoshlarga 30 daqiqalik tekshiruv yuboriladi.")
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supplyvote:"))
    async def supply_vote(callback: CallbackQuery) -> None:
        try:
            user = await callback_user(callback)
            _, poll_id, raw_value = callback.data.split(":")
            async with SessionFactory() as session:
                await cast_supply_vote(session, int(poll_id), user.id, VoteValue(raw_value))
            await callback.answer("Ovozingiz saqlandi.")
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supply:transfer:"))
    async def supply_transfer_start(callback: CallbackQuery) -> None:
        try:
            user = await callback_user(callback)
            task_id = int(callback.data.rsplit(":", 1)[1])
            async with SessionFactory() as session:
                from app.models import SupplyTask

                task = await session.get(SupplyTask, task_id)
                if task is None or task.assigned_user_id != user.id:
                    raise DomainError("Bu vazifa sizga tegishli emas.")
                members = [(member, person) for member, person in await active_supply_queue(session, task.supply_type) if person.id != user.id]
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=person.full_name, callback_data=f"supply:pick:{task_id}:{person.id}")]
                    for _, person in members
                ]
            )
            await callback.message.answer("Kimga o‘tkazmoqchisiz?", reply_markup=markup)
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supply:pick:"))
    async def supply_transfer_pick(callback: CallbackQuery) -> None:
        try:
            sender = await callback_user(callback)
            _, _, task_id, target_id = callback.data.split(":")
            async with SessionFactory() as session:
                request = await create_supply_transfer(session, int(task_id), sender.id, int(target_id))
                target = await session.get(User, request.to_user_id)
            assert target is not None
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Qabul qilaman", callback_data=f"supply:decide:{request.id}:yes"),
                        InlineKeyboardButton(text="❌ Rad etaman", callback_data=f"supply:decide:{request.id}:no"),
                    ]
                ]
            )
            await callback.bot.send_message(
                target.telegram_id,
                f"{sender.full_name} sizga ta’minot navbatini o‘tkazmoqchi. Qabul qilasizmi?",
                reply_markup=markup,
            )
            await callback.answer("So‘rov yuborildi.")
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supply:decide:"))
    async def supply_transfer_decide(callback: CallbackQuery) -> None:
        try:
            recipient = await callback_user(callback)
            _, _, request_id, decision = callback.data.split(":")
            async with SessionFactory() as session:
                request, task = await decide_supply_transfer(session, int(request_id), recipient.id, decision == "yes")
                sender = await session.get(User, request.from_user_id)
            if request.status == SupplyTransferStatus.ACCEPTED:
                await callback.message.edit_text("✅ Navbatni qabul qildingiz.")
                await callback.bot.send_message(recipient.telegram_id, "✅ Ta’minot navbati endi sizda.")
                if sender:
                    await callback.bot.send_message(sender.telegram_id, "✅ Navbatingiz qabul qilindi.")
            else:
                await callback.message.edit_text("❌ Navbatni qabul qilmadingiz.")
                if sender:
                    await callback.bot.send_message(sender.telegram_id, "Navbatingiz qabul qilinmadi.")
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.message(F.text == HISTORY)
    async def history(message: Message) -> None:
        user = await user_for_message(message)
        if user is None:
            return
        async with SessionFactory() as session:
            rows = await session.execute(
                select(FoodAssignment, User)
                .join(User, User.id == FoodAssignment.assigned_user_id)
                .order_by(FoodAssignment.duty_date.desc())
                .limit(10)
            )
            items = list(rows.all())
        if not items:
            await message.answer("Tarix hali bo‘sh.")
            return
        labels = {"active": "jarayonda", "completed": "bajardi", "not_completed": "bajarmadi"}
        text = "📜 Oxirgi navbatlar:\n" + "\n".join(
            f"{assignment.duty_date}: {person.full_name} — {labels[assignment.status.value]}"
            for assignment, person in items
        )
        await message.answer(text)

    @router.message(F.text == TRANSFER)
    async def transfer_start(message: Message, state: FSMContext) -> None:
        user = await user_for_message(message)
        if user is None:
            return
        async with SessionFactory() as session:
            assignment = await current_assignment(session, today_local())
            if assignment is None or assignment.assigned_user_id != user.id:
                await message.answer("Faqat bugungi navbatchi o‘z navbatini o‘tkaza oladi.")
                return
            members = [(member, person) for member, person in await active_queue(session) if person.id != user.id]
        if not members:
            await message.answer("Navbatni o‘tkazish uchun boshqa faol xonadosh yo‘q.")
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=person.full_name, callback_data=f"transfer:pick:{person.id}")]
                for _, person in members
            ]
        )
        await state.update_data(assignment_id=assignment.id)
        await message.answer("Kimga o‘tkazmoqchisiz?", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("transfer:pick:"))
    async def transfer_pick(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            user = await callback_user(callback)
            target_id = int(callback.data.rsplit(":", 1)[1])
            data = await state.get_data()
            if not data.get("assignment_id"):
                raise DomainError("Transfer jarayonini qaytadan boshlang.")
            async with SessionFactory() as session:
                assignment = await session.get(FoodAssignment, data["assignment_id"])
                if assignment is None or assignment.assigned_user_id != user.id:
                    raise DomainError("Bu navbat endi sizga tegishli emas.")
                target = await session.get(User, target_id)
                if target is None or not await is_queue_member(session, target_id) or target_id == user.id:
                    raise DomainError("Faol xonadoshni tanlang.")
            await state.update_data(target_id=target_id)
            await state.set_state(TransferStates.waiting_for_comment)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Izohsiz davom etish", callback_data="transfer:skip")]]
            )
            await callback.message.answer("Sabab yoki izoh yozing. Istamasangiz tugmani bosing.", reply_markup=markup)
        except DomainError as error:
            await callback.answer(str(error), show_alert=True)
        else:
            await callback.answer()

    async def submit_transfer(
        message: Message, state: FSMContext, comment: str | None, actor: User | None = None
    ) -> None:
        data = await state.get_data()
        user = actor or await user_for_message(message)
        if user is None or not data.get("assignment_id") or not data.get("target_id"):
            await state.clear()
            await message.answer("Transfer jarayoni bekor qilindi.")
            return
        try:
            async with SessionFactory() as session:
                assignment = await session.get(FoodAssignment, data["assignment_id"])
                assert assignment is not None
                request = await create_transfer_request(session, assignment, user.id, data["target_id"], comment)
                target = await session.get(User, request.to_user_id)
                assert target is not None
            reason = f"\nIzoh: {comment}" if comment else ""
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Qabul qilaman", callback_data=f"transfer:decide:{request.id}:yes"),
                        InlineKeyboardButton(text="❌ Rad etaman", callback_data=f"transfer:decide:{request.id}:no"),
                    ]
                ]
            )
            await message.bot.send_message(
                target.telegram_id,
                f"{user.full_name} bugungi ovqat navbatini sizga o‘tkazmoqchi.{reason}\nQabul qilasizmi?",
                reply_markup=markup,
            )
            await message.answer("So‘rov yuborildi. Javob kutilmoqda.")
        except DomainError as error:
            await message.answer(str(error))
        finally:
            await state.clear()

    @router.message(TransferStates.waiting_for_comment)
    async def transfer_comment(message: Message, state: FSMContext) -> None:
        await submit_transfer(message, state, message.text[:1000] if message.text else None)

    @router.callback_query(TransferStates.waiting_for_comment, F.data == "transfer:skip")
    async def transfer_skip(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        # Reuse the callback's message as the response channel.
        await submit_transfer(callback.message, state, None, actor=await callback_user(callback))

    @router.callback_query(F.data.startswith("transfer:decide:"))
    async def transfer_decide(callback: CallbackQuery) -> None:
        try:
            recipient = await callback_user(callback)
            _, _, request_id, decision = callback.data.split(":")
            async with SessionFactory() as session:
                request, assignment = await decide_transfer(session, int(request_id), recipient.id, decision == "yes")
                sender = await session.get(User, request.from_user_id)
            if request.status == TransferStatus.ACCEPTED:
                await callback.message.edit_text("✅ Siz bugungi ovqat navbatini qabul qildingiz.")
                await callback.bot.send_message(recipient.telegram_id, "🍽 Bugungi ovqat navbati endi sizda.")
                if sender:
                    await callback.bot.send_message(sender.telegram_id, "✅ Navbatingiz qabul qilindi.")
            else:
                await callback.message.edit_text("❌ Navbatni qabul qilmadingiz.")
                if sender:
                    await callback.bot.send_message(
                        sender.telegram_id, "Navbatingiz qabul qilinmadi. O‘zingiz bajaring yoki boshqa odamni tanlang."
                    )
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("vote:"))
    async def vote(callback: CallbackQuery) -> None:
        try:
            voter = await callback_user(callback)
            _, poll_id, raw_value = callback.data.split(":")
            value = VoteValue(raw_value)
            async with SessionFactory() as session:
                await cast_vote(session, int(poll_id), voter.id, value)
            await callback.answer("Ovozingiz saqlandi. Kerak bo‘lsa tugmani qayta bosib o‘zgartira olasiz.")
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.message(F.text == ADMIN)
    async def admin(message: Message) -> None:
        user = await user_for_message(message)
        if user is None or not user.is_admin:
            await message.answer("Bu bo‘lim faqat admin uchun.")
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📊 Umumiy holat", callback_data="adm:overview")],
                [InlineKeyboardButton(text="👥 Qatnashchilar", callback_data="adm:members")],
                [
                    InlineKeyboardButton(text="🍽 Ovqat navbati", callback_data="adm:queue"),
                    InlineKeyboardButton(text="📦 Non va suv", callback_data="supadm:menu"),
                ],
                [InlineKeyboardButton(text="▶️ Navbatni boshlash", callback_data="adm:start")],
                [InlineKeyboardButton(text="📍 Bugungi navbatchini almashtirish", callback_data="adm:today")],
            ]
        )
        await message.answer("Admin boshqaruvi:", reply_markup=keyboard)

    @router.callback_query(F.data == "adm:overview")
    async def admin_overview(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            async with SessionFactory() as session:
                food = await current_text(session, today_local())
                bread = await supply_status_text(session, SupplyType.BREAD)
                water = await supply_status_text(session, SupplyType.WATER)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🍽 Ovqat navbati", callback_data="adm:queue")],
                    [InlineKeyboardButton(text="📦 Non va suv navbati", callback_data="supadm:menu")],
                    [InlineKeyboardButton(text="👥 Qatnashchilar", callback_data="adm:members")],
                ]
            )
            await callback.message.answer(
                f"📊 Admin holati\n\n{food}\n\n{bread}\n{water}", reply_markup=markup
            )
            await callback.answer()
        except DomainError as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data == "supadm:menu")
    async def supply_admin_menu(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🥖 Non navbati", callback_data="supadm:show:bread")],
                    [InlineKeyboardButton(text="💧 Suv navbati", callback_data="supadm:show:water")],
                ]
            )
            await callback.message.answer("Qaysi navbatni boshqarasiz?", reply_markup=markup)
            await callback.answer()
        except DomainError as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supadm:show:"))
    async def supply_admin_show(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            supply_type = SupplyType(callback.data.rsplit(":", 1)[1])
            label = "🥖 Non" if supply_type == SupplyType.BREAD else "💧 Suv"
            async with SessionFactory() as session:
                entries = await active_supply_queue(session, supply_type)
                status = await supply_status_text(session, supply_type)
                active = await session.get(SupplyActiveTask, supply_type)
                active_task = await session.get(SupplyTask, active.task_id) if active else None
            buttons = [
                [
                    InlineKeyboardButton(text="👥 Qatnashchilar", callback_data=f"supadm:members:{supply_type.value}"),
                    InlineKeyboardButton(text="🎯 Keyingi odam", callback_data=f"supadm:current:{supply_type.value}"),
                ]
            ]
            for index, (_, person) in enumerate(entries, start=1):
                buttons.append(
                    [
                        InlineKeyboardButton(text=f"{index}. {person.full_name}", callback_data="ignore"),
                        InlineKeyboardButton(text="⬆️", callback_data=f"supadm:up:{supply_type.value}:{person.id}"),
                        InlineKeyboardButton(text="⬇️", callback_data=f"supadm:down:{supply_type.value}:{person.id}"),
                    ]
                )
            if active_task and active_task.status == SupplyTaskStatus.AWAITING_DELIVERY:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text="📍 Faol vazifani almashtirish",
                            callback_data=f"supadm:reassign:{supply_type.value}:{active_task.id}",
                        )
                    ]
                )
            order = "Navbat hali tuzilmagan." if not entries else "Tartibni boshqaring yoki keyingi odamni tanlang."
            text = f"{label} navbati\n\n{status}\n\n{order}"
            await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supadm:reassign:"))
    async def supply_admin_reassign_options(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            _, _, raw_type, raw_task_id = callback.data.split(":")
            supply_type = SupplyType(raw_type)
            async with SessionFactory() as session:
                entries = await active_supply_queue(session, supply_type)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=person.full_name,
                            callback_data=f"supadm:settask:{supply_type.value}:{raw_task_id}:{person.id}",
                        )
                    ]
                    for _, person in entries
                ]
            )
            await callback.message.answer("Faol vazifani kimga berasiz?", reply_markup=markup)
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supadm:settask:"))
    async def supply_admin_reassign_confirm(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            _, _, raw_type, raw_task_id, raw_user_id = callback.data.split(":")
            async with SessionFactory() as session:
                task = await reassign_supply_task(session, int(raw_task_id), int(raw_user_id))
                person = await session.get(User, task.assigned_user_id)
            await callback.message.answer(
                f"✅ Faol {('non' if raw_type == 'bread' else 'suv')} vazifasi "
                f"{person.full_name if person else 'tanlangan odam'} ga o‘tdi."
            )
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supadm:members:"))
    async def supply_admin_members(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            supply_type = SupplyType(callback.data.rsplit(":", 1)[1])
            async with SessionFactory() as session:
                users = list((await session.scalars(select(User).order_by(User.full_name))).all())
                active_ids = {person.id for _, person in await active_supply_queue(session, supply_type)}
            buttons = [
                [
                    InlineKeyboardButton(
                        text=("➖ " if person.id in active_ids else "➕ ") + person.full_name,
                        callback_data=f"supadm:{'remove' if person.id in active_ids else 'add'}:{supply_type.value}:{person.id}",
                    )
                ]
                for person in users
            ]
            await callback.message.answer("➕ qo‘shish yoki ➖ o‘chirish:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supadm:add:") | F.data.startswith("supadm:remove:"))
    async def supply_admin_member_change(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            _, action, raw_type, raw_user_id = callback.data.split(":")
            async with SessionFactory() as session:
                if action == "add":
                    await add_supply_queue_member(session, SupplyType(raw_type), int(raw_user_id))
                else:
                    await remove_supply_queue_member(session, SupplyType(raw_type), int(raw_user_id))
            await callback.answer("Navbat yangilandi.")
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supadm:current:"))
    async def supply_admin_current(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            supply_type = SupplyType(callback.data.rsplit(":", 1)[1])
            async with SessionFactory() as session:
                entries = await active_supply_queue(session, supply_type)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=person.full_name, callback_data=f"supadm:setcurrent:{supply_type.value}:{person.id}")]
                    for _, person in entries
                ]
            )
            await callback.message.answer("Keyingi navbatchini tanlang:", reply_markup=markup)
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supadm:setcurrent:"))
    async def supply_admin_set_current(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            _, _, raw_type, raw_user_id = callback.data.split(":")
            async with SessionFactory() as session:
                await set_supply_current_user(session, SupplyType(raw_type), int(raw_user_id))
            await callback.answer("Keyingi navbatchi saqlandi.")
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("supadm:up:") | F.data.startswith("supadm:down:"))
    async def supply_admin_move(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            _, direction, raw_type, raw_user_id = callback.data.split(":")
            async with SessionFactory() as session:
                await move_supply_queue_member(
                    session, SupplyType(raw_type), int(raw_user_id), -1 if direction == "up" else 1
                )
            await callback.answer("Navbat tartibi yangilandi.")
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data == "adm:members")
    async def admin_members(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            async with SessionFactory() as session:
                users = list((await session.scalars(select(User).order_by(User.full_name))).all())
                active_ids = {person.id for _, person in await active_queue(session)}
            buttons = []
            for person in users:
                if person.id in active_ids:
                    buttons.append([InlineKeyboardButton(text=f"➖ {person.full_name}", callback_data=f"adm:remove:{person.id}")])
                else:
                    buttons.append([InlineKeyboardButton(text=f"➕ {person.full_name}", callback_data=f"adm:add:{person.id}")])
            if not buttons:
                await callback.message.answer("Hali hech kim /start yubormagan.")
            else:
                await callback.message.answer("➕ qo‘shish yoki ➖ o‘chirish:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            await callback.answer()
        except DomainError as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("adm:add:") | F.data.startswith("adm:remove:"))
    async def admin_member_change(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            _, action, raw_user_id = callback.data.split(":")
            async with SessionFactory() as session:
                if action == "add":
                    await add_queue_member(session, int(raw_user_id))
                    text = "Qatnashchi navbatga qo‘shildi."
                else:
                    await remove_queue_member(session, int(raw_user_id))
                    text = "Qatnashchi navbatdan o‘chirildi."
            await callback.answer(text)
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data == "adm:queue")
    async def admin_queue(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            async with SessionFactory() as session:
                entries = await active_queue(session)
            if not entries:
                await callback.message.answer("Navbat bo‘sh. Avval qatnashchilarni qo‘shing.")
            else:
                buttons = []
                for index, (_, person) in enumerate(entries, start=1):
                    buttons.append(
                        [
                            InlineKeyboardButton(text=f"{index}. {person.full_name}", callback_data="ignore"),
                            InlineKeyboardButton(text="⬆️", callback_data=f"adm:up:{person.id}"),
                            InlineKeyboardButton(text="⬇️", callback_data=f"adm:down:{person.id}"),
                        ]
                    )
                await callback.message.answer("Navbat tartibi:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            await callback.answer()
        except DomainError as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("adm:up:") | F.data.startswith("adm:down:"))
    async def admin_move(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            _, direction, raw_user_id = callback.data.split(":")
            async with SessionFactory() as session:
                await move_queue_member(session, int(raw_user_id), -1 if direction == "up" else 1)
            await callback.answer("Navbat tartibi yangilandi.")
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data == "adm:start")
    async def admin_start_options(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            async with SessionFactory() as session:
                if await get_assignment_for_date(session, today_local()):
                    raise DomainError("Bugungi navbat allaqachon yaratilgan.")
                entries = await active_queue(session)
            buttons = [
                [InlineKeyboardButton(text=person.full_name, callback_data=f"adm:startuser:{person.id}")]
                for _, person in entries
            ]
            await callback.message.answer("Bugungi birinchi navbatchini tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            await callback.answer()
        except DomainError as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("adm:startuser:"))
    async def admin_start_confirm(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            user_id = int(callback.data.rsplit(":", 1)[1])
            async with SessionFactory() as session:
                assignment = await create_initial_assignment(session, today_local(), user_id)
                person = await session.get(User, assignment.assigned_user_id)
            await callback.message.answer(f"Navbat boshlandi. Bugun: {person.full_name}")
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data == "adm:today")
    async def admin_today_options(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            async with SessionFactory() as session:
                assignment = await current_assignment(session, today_local())
                if assignment is None:
                    raise DomainError("Bugungi navbat hali boshlanmagan.")
                entries = await active_queue(session)
            buttons = [
                [InlineKeyboardButton(text=person.full_name, callback_data=f"adm:settoday:{person.id}")]
                for _, person in entries
            ]
            await callback.message.answer("Bugungi navbatchini tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            await callback.answer()
        except DomainError as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data.startswith("adm:settoday:"))
    async def admin_set_today(callback: CallbackQuery) -> None:
        try:
            await require_admin(callback)
            user_id = int(callback.data.rsplit(":", 1)[1])
            async with SessionFactory() as session:
                assignment = await current_assignment(session, today_local())
                if assignment is None:
                    raise DomainError("Bugungi navbat hali boshlanmagan.")
                await reassign_today(session, assignment, user_id)
                person = await session.get(User, user_id)
            await callback.message.answer(f"Bugungi navbatchi o‘zgardi: {person.full_name}")
            await callback.answer()
        except (DomainError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)

    @router.callback_query(F.data == "ignore")
    async def ignore(callback: CallbackQuery) -> None:
        await callback.answer()

    return router
