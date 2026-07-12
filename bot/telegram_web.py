"""Small HTTPS-proxy target for completing Telegram QR logins with 2FA."""
from __future__ import annotations

import os

from aiohttp import web

from bot.telegram_user import TelegramUserError, two_factor_html


class TelegramTwoFactorServer:
    def __init__(self, service, bot=None) -> None:
        self.service = service
        self.bot = bot
        self.host = os.getenv("TELEGRAM_2FA_CALLBACK_HOST", "127.0.0.1")
        self.port = int(os.getenv("TELEGRAM_2FA_CALLBACK_PORT", "8094"))
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        if not self.service._two_factor_configured():
            return
        app = web.Application()
        app.router.add_get("/telegram/2fa/{token}", self.get)
        app.router.add_post("/telegram/2fa/{token}", self.post)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def get(self, _request: web.Request) -> web.Response:
        return web.Response(text=two_factor_html(), content_type="text/html")

    async def post(self, request: web.Request) -> web.Response:
        try:
            result = await self.service.complete_two_factor_login(request.match_info["token"], (await request.post()).get("password", ""))
        except TelegramUserError as exc:
            return web.Response(text=two_factor_html(str(exc)), content_type="text/html", status=400)
        if self.bot:
            await self.bot.send_message(
                result["telegram_user_id"],
                "Личный Telegram подключён. Открой /telegram_privacy, чтобы подтвердить или отклонить сохранение полной переписки с контактами.",
            )
        return web.Response(text=two_factor_html("Telegram подключён. Можно вернуться в бот."), content_type="text/html")
