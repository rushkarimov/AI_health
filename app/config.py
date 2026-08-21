"""Конфигурация из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_user_ids: list[int]

    yandex_api_key: str
    yandex_folder_id: str
    yandex_llm_model: str
    yandex_vision_model: str

    stt_provider: str
    vision_provider: str
    llm_provider: str

    openai_api_key: str
    anthropic_api_key: str
    routerai_api_key: str
    tavily_api_key: str
    bot_username: str
    brave_api_key: str
    routerai_vision_model: str
    routerai_llm_model: str
    routerai_stt_model: str

    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str

    health_api_token: str
    # Код-приглашение для регистрации через мини-апп. Пустой = регистрация
    # выключена: забытая переменная не должна открывать бота всем желающим.
    invite_code: str
    # Адрес страницы мини-аппа. Телеграм требует HTTPS — по http кнопка
    # web_app не откроется. Пустой = кнопку регистрации не показываем.
    webapp_url: str
    api_port: int

    threshold_photo: float
    threshold_voice: float
    combine_window_sec: int

    # планировщик
    tz: str
    digest_hour: int
    digest_minute: int
    evening_hour: int
    evening_minute: int
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str

    weigh_reminder_days: int
    insight_weekday: int          # 0=понедельник, для еженедельного разбора
    scheduler_enabled: bool

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )

    def threshold_for(self, source: str) -> float:
        """Порог уточнения зависит от канала: фото врёт по порции чаще голоса."""
        return self.threshold_photo if source == "photo" else self.threshold_voice


@lru_cache
def get_settings() -> Settings:
    return Settings(
        bot_token=os.environ.get("BOT_TOKEN", ""),
        allowed_user_ids=_int_list(os.environ.get("ALLOWED_USER_IDS", "")),
        yandex_api_key=os.environ.get("YANDEX_API_KEY", ""),
        yandex_folder_id=os.environ.get("YANDEX_FOLDER_ID", ""),
        # Flash: в ~5 раз дешевле флагмана, для парсинга фраз и справочника
        # нутриентов этого достаточно. Доступен только через OpenAI-эндпоинт.
        yandex_llm_model=os.environ.get("YANDEX_LLM_MODEL", "aliceai-llm-flash"),
        yandex_vision_model=os.environ.get("YANDEX_VISION_MODEL", "aliceai-llm-flash"),
        stt_provider=os.environ.get("STT_PROVIDER", "yandex"),
        vision_provider=os.environ.get("VISION_PROVIDER", "yandex"),
        llm_provider=os.environ.get("LLM_PROVIDER", "yandex"),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        # RouterAI — шлюз к OpenAI/Google/Anthropic с оплатой картой РФ.
        # Имя переменной как в .env, регистр важен.
        routerai_api_key=os.environ.get("RouterAI_API_KEY", ""),
        # поиск в интернете: без ключа работает бесплатный DuckDuckGo
        tavily_api_key=os.environ.get("TAVILY_API_KEY", ""),
        # для ссылки t.me/<bot>?start=auth_... во входе по телефону
        bot_username=os.environ.get("BOT_USERNAME", "AI_health_whoop_bot"),
        brave_api_key=os.environ.get("BRAVE_API_KEY", ""),
        # Flash-Lite по биллингу RouterAI: 0.020 ₽ за разбор фото против
        # 0.074 у Flash. У gpt-5-nano вход экономнее (606 токенов против 1639),
        # но reasoning на выходе гуляет 202-2078 — цена скачет в 8 раз, и
        # иногда ответ приходит пустым.
        routerai_vision_model=os.environ.get(
            "ROUTERAI_VISION_MODEL", "google/gemini-2.5-flash-lite"
        ),
        routerai_llm_model=os.environ.get(
            "ROUTERAI_LLM_MODEL", "google/gemini-2.5-flash-lite"
        ),
        # Whisper Turbo: 0.07 ₽/мин против 0.16 ₽ за 15 сек у SpeechKit.
        # Понимает ogg из Телеграма и не ограничен 30 секундами.
        routerai_stt_model=os.environ.get(
            "ROUTERAI_STT_MODEL", "openai/whisper-large-v3-turbo"
        ),
        pg_host=os.environ.get("POSTGRES_HOST", "localhost"),
        pg_port=int(os.environ.get("POSTGRES_PORT", "5432")),
        pg_db=os.environ.get("POSTGRES_DB", "healthbot"),
        pg_user=os.environ.get("POSTGRES_USER", "healthbot"),
        pg_password=os.environ.get("POSTGRES_PASSWORD", "healthbot"),
        health_api_token=os.environ.get("HEALTH_API_TOKEN", ""),
        invite_code=os.environ.get("INVITE_CODE", ""),
        webapp_url=os.environ.get("WEBAPP_URL", ""),
        api_port=int(os.environ.get("API_PORT", "8080")),
        threshold_photo=float(os.environ.get("CONFIDENCE_THRESHOLD_PHOTO", "0.75")),
        threshold_voice=float(os.environ.get("CONFIDENCE_THRESHOLD_VOICE", "0.5")),
        combine_window_sec=int(os.environ.get("COMBINE_WINDOW_SEC", "90")),
        tz=os.environ.get("TZ", "Europe/Moscow"),
        digest_hour=int(os.environ.get("DIGEST_HOUR", "12")),
        digest_minute=int(os.environ.get("DIGEST_MINUTE", "0")),
        evening_hour=int(os.environ.get("EVENING_HOUR", "0")),
        evening_minute=int(os.environ.get("EVENING_MINUTE", "0")),
        langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
        # из контейнера бота Langfuse доступен по имени сервиса в своей сети
        langfuse_host=os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000"),
        weigh_reminder_days=int(os.environ.get("WEIGH_REMINDER_DAYS", "7")),
        insight_weekday=int(os.environ.get("INSIGHT_WEEKDAY", "0")),
        scheduler_enabled=os.environ.get("SCHEDULER_ENABLED", "true").lower()
        not in {"0", "false", "no"},
    )
