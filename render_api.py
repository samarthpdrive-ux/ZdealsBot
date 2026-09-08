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


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    deposit_task: asyncio.Task | None = None
    polling_task: asyncio.Task | None = None
    delivery_polling_task: asyncio.Task | None = None

    try:
        # ----------------------------------------------------
        # Main Telegram bot
        # ----------------------------------------------------

        await bot.delete_webhook(drop_pending_updates=True)

        me = await bot.get_me()

        logger.info(
            "Main Telegram bot logged in as @%s",
            me.username,
        )

        # ----------------------------------------------------
        # Deposit checker
        # ----------------------------------------------------

        deposit_task = asyncio.create_task(
            deposit_checker_loop(),
            name="deposit-checker",
        )

        # ----------------------------------------------------
        # Main bot polling
        # ----------------------------------------------------

        polling_task = asyncio.create_task(
            dp.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
            ),
            name="telegram-polling",
        )

        # ----------------------------------------------------
        # Delivery bot
        # ----------------------------------------------------

        if delivery_bot and delivery_dp:

            await delivery_bot.delete_webhook(
                drop_pending_updates=False
            )

            delivery_polling_task = asyncio.create_task(
                delivery_dp.start_polling(
                    delivery_bot,
                    handle_signals=False,
                    close_bot_session=False,
                ),
                name="delivery-bot-polling",
            )

            logger.info(
                "Separate manual Delivery Bot started"
            )

        logger.info(
            "Telegram polling started"
        )

        logger.info(
            "Private reseller API started"
        )

        logger.info(
            "Available internal API: /internal/v1/products"
        )

        yield

    finally:

        # ----------------------------------------------------
        # Stop background tasks
        # ----------------------------------------------------

        for task in (
            delivery_polling_task,
            polling_task,
            deposit_task,
        ):
            if task and not task.done():
                task.cancel()

        for task in (
            delivery_polling_task,
            polling_task,
            deposit_task,
        ):
            if task:
                with suppress(asyncio.CancelledError):
                    await task

        # ----------------------------------------------------
        # Close sessions
        # ----------------------------------------------------

        if bot.session:
            await bot.session.close()

        if delivery_bot and delivery_bot.session:
            await delivery_bot.session.close()

        logger.info(
            "Application shutdown complete"
        )


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="NomanBot Reseller API",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


# ============================================================
# PRIVATE RESELLER ROUTER
# ============================================================

app.include_router(
    internal_reseller_router
)


# ============================================================
# ROOT
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "NomanBot Reseller API",
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "NomanBot Reseller API",
    }


# ============================================================
# ROUTE DEBUG
# ============================================================
#
# TEMPORARY DIAGNOSTIC ROUTE.
# Remove after everything works.
#
# ============================================================

@app.get("/debug-routes")
async def debug_routes():

    routes = []

    for route in app.routes:

        methods = getattr(
            route,
            "methods",
            None,
        )

        routes.append(
            {
                "path": getattr(
                    route,
                    "path",
                    "",
                ),
                "methods": sorted(
                    methods or []
                ),
            }
        )

    return {
        "status": "ok",
        "routes": routes,
    }


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    logger.info(
        "Starting NomanBot Reseller API on port %s",
        port,
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
