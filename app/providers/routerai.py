"""RouterAI — шлюз к моделям OpenAI, Google, Anthropic с оплатой картой РФ.

Нужен ради одного: **распознавания еды по фото**. У Яндекса такой модели нет
вообще — yandexgpt картинку молча игнорирует, Flash отдаёт 500, а Vision OCR
читает только текст на изображении.

API совместим с OpenAI, поэтому используем их SDK, меняя base_url.

Про выбор модели. Проверено на живом API 01.08.2026, одна и та же задача
разбора фото:

    google/gemini-2.5-flash    86-115 выходных токенов, reasoning 0
    openai/gpt-5-nano         1608-2078 токенов, из них 1536-1984 reasoning

Nano тратит в 15-20 раз больше на тот же результат: почти всё уходит в скрытые
размышления. `reasoning_effort="low"` снижает до ~450, но всё равно вчетверо
дороже Gemini, и объём reasoning плавает (320-448) — при жёстком max_tokens
ответ иногда обрезается в пустоту. Поэтому по умолчанию Gemini Flash.
"""
from __future__ import annotations

import base64
import logging

from app.config import get_settings
from app.providers.base import Recognition
from app.providers.parsing import extract_json
from app.prompts import LABS_PARSE_SYSTEM, LABS_PARSE_USER, PHOTO_SYSTEM, PHOTO_USER
from app.tracing import trace_llm

log = logging.getLogger(__name__)

BASE_URL = "https://routerai.ru/api/v1"

# Модели с reasoning: им нужен запас токенов и явное ограничение усилий,
# иначе вся квота уходит в размышления и content приходит пустым.
_REASONING_MODELS = ("gpt-5", "o1", "o3", "o4")


def _client():
    import os

    from openai import AsyncOpenAI

    s = get_settings()
    if not s.routerai_api_key:
        raise RuntimeError(
            "RouterAI_API_KEY не задан в .env — распознавание фото недоступно"
        )
    client = AsyncOpenAI(api_key=s.routerai_api_key, base_url=BASE_URL, timeout=90.0)

    # LangSmith: обёртка ловит все chat.completions (включая стримы) и кладёт
    # их LLM-ранами внутрь текущего графа — без неё в LangSmith были видны
    # только узлы LangGraph, а сами вызовы моделей отсутствовали
    if os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"):
        try:
            from langsmith.wrappers import wrap_openai
            client = wrap_openai(client)
        except Exception:
            log.exception("LangSmith wrap_openai не подключился")
    return client


def _is_reasoning(model: str) -> bool:
    return any(p in model.lower() for p in _REASONING_MODELS)


class RouterAiStt:
    """Распознавание речи. Whisper Large V3 Turbo: 0.07 ₽ за минуту против
    0.16 ₽ за каждые 15 секунд у SpeechKit — примерно в 10 раз дешевле.

    Формат ogg/opus поддерживается нативно, поэтому голосовые Телеграма
    перекодировать не нужно. И нет ограничения SpeechKit v1 на 30 секунд.
    """

    async def transcribe(self, audio: bytes, mime: str = "audio/ogg") -> str:
        model = get_settings().routerai_stt_model
        ext = "ogg" if "ogg" in mime else "wav"

        async with trace_llm(
            name="stt",
            model=model,
            messages=[{"role": "user", "content": f"[аудио {len(audio)} байт]"}],
            provider="routerai",
        ) as finish:
            client = _client()
            r = await client.audio.transcriptions.create(
                model=model,
                file=(f"voice.{ext}", audio, mime),
                language="ru",
            )
            text = (r.text or "").strip()
            finish(output=text)
        return text


class RouterAiVision:
    """Распознавание состава блюда по фото."""

    async def recognize_food(self, image: bytes, caption: str | None = None) -> Recognition:
        model = get_settings().routerai_vision_model
        b64 = base64.b64encode(image).decode()

        # в PHOTO_USER есть плейсхолдер {caption} — без format модель получала
        # буквальный текст "{caption}" и отвечала пустым items
        user_text = PHOTO_USER.format(caption=caption or "нет")

        messages = [
            {"role": "system", "content": PHOTO_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ]

        kwargs: dict = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            # с запасом: у reasoning-моделей часть уходит в скрытые размышления
            "max_tokens": 3000 if _is_reasoning(model) else 800,
        }
        if _is_reasoning(model):
            kwargs["reasoning_effort"] = "low"

        # в трейс отдаём messages без картинки: base64 на сотни килобайт
        # раздувает трейс и ничего не проясняет
        traced = [
            {"role": "system", "content": PHOTO_SYSTEM},
            {"role": "user", "content": f"{user_text}\n[изображение {len(image)} байт]"},
        ]

        async with trace_llm(
            name="vision", model=model, messages=traced, provider="routerai"
        ) as finish:
            client = _client()
            r = await client.chat.completions.create(**kwargs)
            raw = (r.choices[0].message.content or "").strip()
            finish(output=raw, usage=r.usage.model_dump() if r.usage else None)

        if not raw:
            log.warning("RouterAI %s вернул пустой ответ (модель ушла в reasoning)", model)
            return Recognition(items=[], note="не удалось разобрать фото")

        data = extract_json(raw)
        if not isinstance(data, dict):
            log.warning("RouterAI %s: не JSON: %.200s", model, raw)
            return Recognition(items=[], note="не удалось разобрать фото")
        return Recognition.model_validate(data)


    async def recognize_labs(self, image: bytes) -> str:
        """Читает бланк анализов с фото. Отдаёт сырой JSON — разбирает вызывающий.

        Отдельный метод, а не recognize_food с другим промптом: у анализов
        другой формат ответа (границы нормы, единицы) и не нужен Recognition.
        """
        from datetime import date

        model = get_settings().routerai_vision_model
        b64 = base64.b64encode(image).decode()
        user_text = LABS_PARSE_USER.format(today=date.today().isoformat(), extra="")

        messages = [
            {"role": "system", "content": LABS_PARSE_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ]
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            # Реальный ОАК — 26 строк с длинными названиями: на 2000 токенов
            # JSON обрезался на середине и не парсился вовсе (0 показателей).
            # 8000 хватает на ~60 строк с запасом.
            "max_tokens": 10000 if _is_reasoning(model) else 8000,
        }
        if _is_reasoning(model):
            kwargs["reasoning_effort"] = "low"

        traced = [
            {"role": "system", "content": LABS_PARSE_SYSTEM},
            {"role": "user", "content": f"[бланк анализов {len(image)} байт]"},
        ]
        async with trace_llm(
            name="vision-labs", model=model, messages=traced, provider="routerai"
        ) as finish:
            r = await _client().chat.completions.create(**kwargs)
            out = (r.choices[0].message.content or "").strip()
            finish(output=out, usage=r.usage.model_dump() if r.usage else None)
        return out


class RouterAiLlm:
    """Текстовые запросы — на случай сравнения с Яндексом."""

    async def complete_stream(self, system: str, user: str):
        """Ответ модели кусками, как печатает ChatGPT.

        Без json_mode: стримятся только человекочитаемые тексты
        (рекомендации, разборы) — JSON надо ждать целиком.
        """
        model = get_settings().routerai_llm_model
        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_tokens": 3000 if _is_reasoning(model) else 2000,
            "stream": True,
        }
        if _is_reasoning(model):
            kwargs["reasoning_effort"] = "low"

        stream = await _client().chat.completions.create(**kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        model = get_settings().routerai_llm_model
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 3000 if _is_reasoning(model) else 2000,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
            # JSON-ответы бывают большими (26 строк анализов текстом) —
            # обрезка на лимите ломает парсинг целиком
            kwargs["max_tokens"] = 10000 if _is_reasoning(model) else 6000
        if _is_reasoning(model):
            kwargs["reasoning_effort"] = "low"

        async with trace_llm(
            name="llm", model=model, messages=messages, provider="routerai"
        ) as finish:
            client = _client()
            r = await client.chat.completions.create(**kwargs)
            out = r.choices[0].message.content or ""
            finish(output=out, usage=r.usage.model_dump() if r.usage else None)
        return out
