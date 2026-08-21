"""Фабрика провайдеров: читает .env и отдаёт нужную реализацию.

Сейчас реализован Яндекс. Для openai/anthropic оставлены явные заглушки —
когда захочешь сравнить качество, дописывается один класс, а граф и хендлеры
не меняются вообще.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.providers.base import LlmProvider, SttProvider, VisionProvider
from app.providers.yandex import YandexLlm, YandexStt


class ProviderNotImplemented(RuntimeError):
    pass


@lru_cache
def get_stt() -> SttProvider:
    name = get_settings().stt_provider
    if name == "routerai":
        from app.providers.routerai import RouterAiStt

        return RouterAiStt()
    if name == "yandex":
        return YandexStt()
    raise ProviderNotImplemented(
        f"STT_PROVIDER={name} пока не реализован. Реализованы: routerai, yandex. "
        "Добавь класс в app/providers/ и зарегистрируй здесь."
    )


@lru_cache
def get_vision() -> VisionProvider:
    name = get_settings().vision_provider
    if name == "routerai":
        from app.providers.routerai import RouterAiVision

        return RouterAiVision()
    if name == "yandex":
        # у Яндекса нет модели, распознающей еду по фото — вернёт понятную
        # ошибку с подсказкой переключиться
        return YandexLlm()
    raise ProviderNotImplemented(
        f"VISION_PROVIDER={name} пока не реализован. "
        "Реализованы: routerai (фото работает), yandex (фото не работает)."
    )


@lru_cache
def get_llm() -> LlmProvider:
    name = get_settings().llm_provider
    if name == "routerai":
        from app.providers.routerai import RouterAiLlm

        return RouterAiLlm()
    if name == "yandex":
        return YandexLlm()
    raise ProviderNotImplemented(
        f"LLM_PROVIDER={name} пока не реализован. Реализован: yandex."
    )
