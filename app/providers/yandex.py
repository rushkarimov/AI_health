"""Провайдеры Yandex Cloud: SpeechKit (STT) и LLM (текст).

Авторизация — API-ключ сервисного аккаунта (Api-Key), самый простой вариант:
не требует обмена на IAM-токен и не истекает через 12 часов.

Про выбор эндпоинта. У Яндекса их два, и они не равнозначны:

  * `foundationModels/v1/completion` — нативный. Работает с yandexgpt,
    но Alice AI LLM Flash через него отдаёт
    "Model is not available via gRPC API. Please use HTTP OpenAI API".
  * `/v1/chat/completions` — OpenAI-совместимый. Работает и с yandexgpt,
    и с aliceai-llm-flash. Модель задаётся полным URI gpt://<folder>/<model>;
    короткое имя даёт "Failed to parse model URI".

Поэтому используем второй — он покрывает обе модели.

Проверено на живом API 30.07.2026: у Flash работает текст и response_format
(строгий JSON), но любая картинка возвращает 500. Распознавание еды по фото
на Яндексе недоступно — фото-канал уходит на другого провайдера.
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.tracing import trace_llm

log = logging.getLogger(__name__)

STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
LLM_URL = "https://llm.api.cloud.yandex.net/v1/chat/completions"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _auth_headers() -> dict[str, str]:
    s = get_settings()
    return {
        "Authorization": f"Api-Key {s.yandex_api_key}",
        # OpenAI-совместимый эндпоинт требует каталог отдельным заголовком
        "x-folder-id": s.yandex_folder_id,
    }


# По первым словам system-промпта понятно, какой это шаг: в интерфейсе Langfuse
# трейсы должны различаться, иначе все одинаково называются "llm".
_TRACE_NAMES = (
    ("классификатор сообщений", "router"),
    ("справочник нутриентов", "nutrients"),
    ("разбираешь фразу о съеденной еде", "parse-voice"),
    ("состав блюда по фотографии", "vision"),
    ("личный аналитик", "agent-plan"),
    ("личный помощник", "agent-answer"),
    ("тренер и аналитик здоровья", "digest-morning"),
    ("вечернюю сводку", "digest-evening"),
    ("разбираешь динамику", "insight"),
    ("отклонился от плана", "alert-weight"),
)


def _trace_name(messages: list[dict]) -> str:
    system = ""
    for m in messages:
        if m.get("role") == "system":
            # переносы строк в промптах ломают поиск подстроки
            system = " ".join(str(m.get("content", "")).lower().split())
            break
    for needle, name in _TRACE_NAMES:
        if needle in system:
            return name
    return "llm"


def _model_uri(model: str) -> str:
    """Полный URI модели — короткое имя эндпоинт не принимает."""
    return f"gpt://{get_settings().yandex_folder_id}/{model}"


class YandexStt:
    """Синхронное распознавание SpeechKit v1.

    Ограничение API: файл до 1 МБ и до 30 секунд. Голосовые из Телеграм
    в OGG/Opus почти всегда влезают; более длинные надо гонять через
    асинхронное распознавание (v3) — вынесено в TODO, не нужно на старте.
    """

    async def transcribe(self, audio: bytes, mime: str = "audio/ogg") -> str:
        s = get_settings()
        params = {
            "folderId": s.yandex_folder_id,
            "lang": "ru-RU",
            # oggopus — родной формат голосовых Telegram, перекодировать не нужно
            "format": "oggopus",
            # включает распознавание чисел словами: "двести грамм" -> "200 грамм"
            "rawResults": "false",
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                STT_URL, params=params, headers=_auth_headers(), content=audio
            )
            r.raise_for_status()
            return r.json().get("result", "").strip()


class YandexVisionUnsupported(RuntimeError):
    """Яндекс не распознаёт еду по фото — ни одна из его моделей.

    Проверено на живом API: yandexgpt принимает картинку, но отвечает
    "нет картинки" (молча игнорирует), aliceai-llm и Flash отдают 500,
    модели yandexgpt-vision не существует. Vision OCR читает только текст
    на изображении, а не содержимое блюда.
    """


class YandexLlm:
    """Текстовые запросы к моделям Яндекса через OpenAI-совместимый эндпоинт."""

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        return await self._call(
            model=get_settings().yandex_llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_mode=json_mode,
        )

    async def recognize_food(self, image: bytes, caption: str | None = None):
        raise YandexVisionUnsupported(
            "Распознавание еды по фото на Яндексе не работает. "
            "Поставь VISION_PROVIDER=anthropic (или openai) в .env."
        )

    async def _call(self, model: str, messages: list[dict], *, json_mode: bool) -> str:
        payload: dict = {
            "model": _model_uri(model),
            "messages": messages,
            # для парсинга нужна детерминированность, не креатив
            "temperature": 0.1,
            "max_tokens": 2000,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        # единственная точка, через которую идут все текстовые вызовы, —
        # поэтому трейсинг вешаем здесь, а не в каждом вызывающем модуле
        async with trace_llm(
            name=_trace_name(messages),
            model=model,
            messages=messages,
            json_mode=json_mode,
        ) as finish:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(LLM_URL, headers=_auth_headers(), json=payload)
                if r.status_code >= 400:
                    log.error("Yandex LLM %s: %s", r.status_code, r.text[:500])
                r.raise_for_status()
                data = r.json()

            content = data["choices"][0]["message"]["content"]
            finish(output=content, usage=data.get("usage"))
            return content
