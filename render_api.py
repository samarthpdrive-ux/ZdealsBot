"""Render entry point: Telegram polling plus the private Wasmer API bridge.

Run on Render with: python render_api.py
The only public reseller API remains the Wasmer URL.  This service rejects
direct bridge requests unless Wasmer sends X-Internal-Bot-Secret.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI

from api.reseller_v1 import router as internal_reseller_router
from bot_app import bot, dp
from delivery_bot_app import delivery_bot, delivery_dp
from services.deposit_checker import deposit_checker_loop


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start long polling and the deposit checker beside FastAPI."""
    deposit_task: asyncio.Task | None = None
    polling_task: asyncio.Task | None = None
    delivery_polling_task: asyncio.Task | None = None
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        me = await bot.get_me()
        logger.info("Logged in as @%s", me.username)

        deposit_task = asyncio.create_task(deposit_checker_loop(), name="deposit-checker")
        polling_task = asyncio.create_task(
            dp.start_polling(bot, handle_signals=False, close_bot_session=False),
            name="telegram-polling",
        )
        if delivery_bot and delivery_dp:
            await delivery_bot.delete_webhook(drop_pending_updates=False)
            delivery_polling_task = asyncio.create_task(
                delivery_dp.start_polling(
                    delivery_bot,
                    handle_signals=False,
                    close_bot_session=False,
                ),
                name="delivery-bot-polling",
            )
            logger.info("Separate manual Delivery Bot started")
        logger.info("Telegram polling and private Wasmer bridge started")
        yield
    finally:
        for task in (delivery_polling_task, polling_task, deposit_task):
            if task and not task.done():
                task.cancel()
        for task in (delivery_polling_task, polling_task, deposit_task):
            if task:
                with suppress(asyncio.CancelledError):
                    await task
        if bot.session:
            await bot.session.close()
        if delivery_bot and delivery_bot.session:
            await delivery_bot.session.close()


app = FastAPI(
    title="NomanBot private delivery service",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# Private only: /internal/v1/*. The router requires X-Internal-Bot-Secret.
app.include_router(internal_reseller_router)


@app.get("/")
async def health() -> dict:
    return {"status": "ok", "service": "NomanBot polling and delivery service"}


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        log_level="info",
    )
