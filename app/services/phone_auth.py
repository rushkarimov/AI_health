"""Вход по номеру телефона: код присылает наш же бот в Телеграм.

Устройство потока:

1. приложение отправляет номер → `start_auth` создаёт заявку и возвращает
   ссылку `t.me/<bot>?start=auth_<token>`;
2. пользователь открывает бота, жмёт Start и кнопку «Поделиться номером» —
   Телеграм отдаёт боту и номер, и chat_id;
3. `confirm_phone` сверяет номер с заявкой и выдаёт код, который бот
   отправляет в этот же чат;
4. приложение вызывает `verify_code` — при совпадении получает долгий
   токен сессии.

Почему не Telegram Gateway API: сообщения от собственного бота бесплатны,
а номер здесь подтверждает сам Телеграм, то есть от руки его не подделать.
Gateway понадобился бы только для тех, кто ни разу не открывал бота, —
им отправить сообщение первым Телеграм не позволяет.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from app.db import repo

log = logging.getLogger(__name__)

# Сколько живёт заявка на вход. Больше — риск, что ссылкой воспользуется
# кто-то другой; меньше — не успеешь переключиться в Телеграм и обратно.
REQUEST_TTL_MIN = 15
# Код действует короче: он уже пришёл в личный чат.
CODE_TTL_MIN = 10
# Попыток ввода кода до отказа: защита от перебора шестизначного числа.
MAX_ATTEMPTS = 5
# Не даём слать заявки чаще, чем раз в столько секунд на один номер.
RESEND_COOLDOWN_SEC = 60


def _norm_phone(raw: str) -> str | None:
    """Приводит номер к виду Телеграма: только цифры, без плюса.

    Телеграм отдаёт `79991234567`. Пользователь может ввести
    «+7 (999) 123-45-67» или «8 999 …» — сводим к одному виду, иначе
    сверка номеров не сойдётся.
    """
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return None
    # 8 в начале российского номера — это та же «семёрка»
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:              # ввели без кода страны
        digits = "7" + digits
    return digits if 10 <= len(digits) <= 15 else None


async def start_auth(raw_phone: str) -> dict[str, Any]:
    """Создаёт заявку на вход. Возвращает ссылку на бота и признак новизны."""
    phone = _norm_phone(raw_phone)
    if not phone:
        return {"ok": False, "error": "Проверь номер телефона"}

    # частота: иначе кнопкой «отправить снова» можно завалить чат кодами
    rows = await repo.fetch(
        """
        SELECT extract(epoch FROM now() - created_at) AS age
        FROM auth_requests WHERE phone = %s ORDER BY created_at DESC LIMIT 1
        """,
        (phone,))
    if rows and rows[0]["age"] is not None and rows[0]["age"] < RESEND_COOLDOWN_SEC:
        wait = int(RESEND_COOLDOWN_SEC - rows[0]["age"])
        return {"ok": False, "error": f"Подожди {wait} сек и попробуй снова"}

    token = secrets.token_urlsafe(16)
    await repo.execute(
        "INSERT INTO auth_requests (token, phone) VALUES (%s, %s)",
        (token, phone))

    # Знакомый номер? Тогда бот может прислать код сразу, без «Поделиться».
    known = await repo.fetch(
        "SELECT user_id FROM users WHERE phone = %s", (phone,))

    from app.config import get_settings
    bot_name = get_settings().bot_username or "AI_health_whoop_bot"
    return {
        "ok": True,
        "token": token,
        "link": f"https://t.me/{bot_name}?start=auth_{token}",
        "known": bool(known),
    }


async def confirm_phone(token: str, tg_user_id: int, tg_phone: str) -> dict[str, Any]:
    """Бот подтвердил номер: сверяем с заявкой и выдаём код.

    Вызывается из обработчика кнопки «Поделиться номером».
    """
    phone = _norm_phone(tg_phone)
    rows = await repo.fetch(
        """
        SELECT phone, extract(epoch FROM now() - created_at) / 60 AS age_min
        FROM auth_requests WHERE token = %s
        """,
        (token,))
    if not rows:
        return {"ok": False, "error": "Ссылка не найдена — начни вход заново"}
    req = rows[0]
    if (req["age_min"] or 0) > REQUEST_TTL_MIN:
        return {"ok": False, "error": "Ссылка устарела — начни вход заново"}
    if req["phone"] != phone:
        # Номер в Телеграме другой: возможно, ввели чужой или опечатались.
        return {"ok": False, "error": "not_match", "expected": req["phone"]}

    code = f"{secrets.choice(range(100000, 1000000))}"
    await repo.execute(
        """
        UPDATE auth_requests SET code = %s, user_id = %s, attempts = 0
        WHERE token = %s
        """,
        (code, tg_user_id, token))

    # привязываем телефон к профилю: со следующего раза код придёт сразу
    await repo.execute(
        "UPDATE users SET phone = %s, updated_at = now() WHERE user_id = %s",
        (phone, tg_user_id))
    return {"ok": True, "code": code}


async def code_for_known(phone_raw: str, token: str) -> dict[str, Any]:
    """Код для знакомого номера: бот пишет в чат без «Поделиться номером»."""
    phone = _norm_phone(phone_raw)
    rows = await repo.fetch(
        "SELECT user_id FROM users WHERE phone = %s", (phone,))
    if not rows:
        return {"ok": False, "error": "Номер не найден"}
    user_id = rows[0]["user_id"]

    code = f"{secrets.choice(range(100000, 1000000))}"
    await repo.execute(
        "UPDATE auth_requests SET code = %s, user_id = %s, attempts = 0 "
        "WHERE token = %s",
        (code, user_id, token))
    return {"ok": True, "code": code, "user_id": user_id}


async def verify_code(token: str, code: str) -> dict[str, Any]:
    """Проверяет код и выдаёт токен сессии."""
    rows = await repo.fetch(
        """
        SELECT code, user_id, attempts,
               extract(epoch FROM now() - created_at) / 60 AS age_min
        FROM auth_requests WHERE token = %s
        """,
        (token,))
    if not rows:
        return {"ok": False, "error": "Заявка не найдена"}
    req = rows[0]

    if not req["code"]:
        return {"ok": False, "error": "Код ещё не отправлен — открой бота"}
    if (req["age_min"] or 0) > CODE_TTL_MIN:
        return {"ok": False, "error": "Код устарел — запроси новый"}
    if (req["attempts"] or 0) >= MAX_ATTEMPTS:
        return {"ok": False, "error": "Слишком много попыток — запроси новый код"}

    if code.strip() != req["code"]:
        await repo.execute(
            "UPDATE auth_requests SET attempts = attempts + 1 WHERE token = %s",
            (token,))
        left = MAX_ATTEMPTS - (req["attempts"] or 0) - 1
        return {"ok": False,
                "error": f"Неверный код, осталось попыток: {max(left, 0)}"}

    session = secrets.token_urlsafe(32)
    await repo.execute(
        "INSERT INTO app_sessions (token, user_id) VALUES (%s, %s)",
        (session, req["user_id"]))
    await repo.execute(
        "UPDATE auth_requests SET confirmed_at = now() WHERE token = %s",
        (token,))
    log.info("Вход по телефону выполнен: user_id=%s", req["user_id"])
    return {"ok": True, "session": session, "user_id": req["user_id"]}


async def user_by_session(token: str) -> int | None:
    """user_id по токену сессии; заодно обновляет время последнего входа."""
    if not token:
        return None
    rows = await repo.fetch(
        "SELECT user_id FROM app_sessions WHERE token = %s", (token,))
    if not rows:
        return None
    await repo.execute(
        "UPDATE app_sessions SET last_seen = now() WHERE token = %s", (token,))
    return rows[0]["user_id"]


async def cleanup() -> None:
    """Чистка просроченных заявок — раз в сутки из планировщика."""
    await repo.execute(
        "DELETE FROM auth_requests WHERE created_at < now() - interval '1 day'")
