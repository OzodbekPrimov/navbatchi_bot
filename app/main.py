import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import get_settings
from app.database import close_database, create_schema
from app.handlers import build_router
from app.scheduler import DutyScheduler


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await create_schema()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    scheduler = DutyScheduler(bot, settings)
    dispatcher.include_router(build_router(settings, scheduler.dispatch_manual_reminders))
    scheduler_task = asyncio.create_task(scheduler.run(), name="duty-scheduler")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler.stop()
        await scheduler_task
        await bot.session.close()
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
