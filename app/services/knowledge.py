"""База знаний: гибридный поиск по научным статьям о здоровье.

Устройство:

* **BGE-M3** (1024 измерения) — плотные векторы, понимает смысл и работает
  с русским наравне с английским: спрашиваем по-русски, находим в
  англоязычных статьях;
* **BM25** — разреженные векторы, ловят точные термины и цифры, которые
  смысловой поиск размывает («ферритин», «VO2max», «1.6 г/кг»);
* **bge-reranker-v2-m3** — пересортировывает найденное. Векторный поиск
  быстрый, но грубый: реранкер читает пару «вопрос-фрагмент» целиком и
  отделяет действительно релевантное.

Модели загружаются лениво, при первом обращении: держать полтора гигабайта
в памяти ради бота, который может ни разу не спросить про статьи, незачем.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

COLLECTION = "health_knowledge"
DENSE_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
DENSE_DIM = 1024

# Сколько кандидатов достаём до реранкера. Замеры на этом сервере: реранк
# 20 пар — 22 секунды, 8 пар — около 9. База небольшая, и нужные фрагменты
# и так попадают в первую восьмёрку, поэтому берём 8.
CANDIDATES = 8
# Сколько фрагментов уходит в промпт после пересортировки.
TOP_K = 4
# Порог реранкера: ниже — фрагмент к вопросу отношения не имеет. Подобран
# по проверке: релевантная пара давала 0.9, посторонняя — 0.0.
MIN_SCORE = 0.15
# Реранкер стоит ~9 секунд на запрос, но именно он отсекает фрагменты не по
# теме: RRF слепо смешивает два ранжирования и мусор наверх пропускает.
RERANK = True

_dense = None
_sparse = None
_reranker = None
_client = None


def _qdrant():
    global _client
    if _client is None:
        import os

        from qdrant_client import QdrantClient

        _client = QdrantClient(
            url=os.environ.get("QDRANT_URL", "http://qdrant:6333"),
            api_key=os.environ.get("QDRANT_API_KEY") or None,
            timeout=30,
        )
    return _client


def _dense_model():
    global _dense
    if _dense is None:
        import torch
        from sentence_transformers import SentenceTransformer

        # без ограничения PyTorch плодит потоки сверх числа ядер и они
        # дерутся за процессор — на 4 ядрах это заметно медленнее
        torch.set_num_threads(4)
        log.info("Загружаю %s…", DENSE_MODEL)
        _dense = SentenceTransformer(DENSE_MODEL)
        # первый прогон компилирует ядра операций и стоит ~15 секунд:
        # платим эту цену при старте, а не в первом вопросе пользователя
        _dense.encode(["прогрев"])
    return _dense


def _sparse_model():
    """BM25 из fastembed: не нейросеть, а счётчик — грузится мгновенно."""
    global _sparse
    if _sparse is None:
        from fastembed import SparseTextEmbedding

        _sparse = SparseTextEmbedding("Qdrant/bm25")
    return _sparse


def _rerank_model():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder

        log.info("Загружаю %s…", RERANK_MODEL)
        # 512 токенов на пару — избыточно: наши фрагменты короче, а время
        # реранка растёт с длиной. 256 покрывает фрагмент целиком.
        _reranker = CrossEncoder(RERANK_MODEL, max_length=256)
        _reranker.predict([("прогрев", "прогрев")])
    return _reranker


def ensure_collection() -> None:
    """Создаёт коллекцию под гибридный поиск, если её ещё нет."""
    from qdrant_client import models

    client = _qdrant()
    if client.collection_exists(COLLECTION):
        return
    client.create_collection(
        COLLECTION,
        vectors_config={
            "dense": models.VectorParams(size=DENSE_DIM,
                                         distance=models.Distance.COSINE),
        },
        sparse_vectors_config={
            # IDF нужен именно для BM25: без него редкие термины не получают
            # веса и поиск по «ферритину» ничем не отличается от поиска по «и»
            "bm25": models.SparseVectorParams(
                modifier=models.Modifier.IDF),
        },
    )
    log.info("Коллекция %s создана", COLLECTION)


def _to_sparse(text: str):
    from qdrant_client import models

    emb = next(_sparse_model().embed([text]))
    return models.SparseVector(indices=emb.indices.tolist(),
                               values=emb.values.tolist())


def add_documents(chunks: list[dict[str, Any]], start_id: int = 0) -> int:
    """Кладёт фрагменты в коллекцию.

    Каждый chunk: {"text": ..., "title": ..., "source": ..., "topic": ...}
    """
    from qdrant_client import models

    if not chunks:
        return 0
    ensure_collection()

    texts = [c["text"] for c in chunks]
    dense = _dense_model().encode(texts, batch_size=8, show_progress_bar=False)

    points = []
    for i, (chunk, vec) in enumerate(zip(chunks, dense)):
        points.append(models.PointStruct(
            id=start_id + i,
            vector={"dense": vec.tolist(), "bm25": _to_sparse(chunk["text"])},
            payload={k: v for k, v in chunk.items()},
        ))
    _qdrant().upsert(COLLECTION, points=points)
    log.info("В базу знаний добавлено фрагментов: %d", len(points))
    return len(points)


def warmup() -> None:
    """Прогрев моделей при старте: первый вызов PyTorch стоит ~15 секунд.

    Без него эту цену платил первый же вопрос пользователя, и ответ
    приходил через минуту вместо пары секунд.
    """
    try:
        _dense_model()
        if RERANK:
            _rerank_model()
        _sparse_model()
        log.info("Модели базы знаний прогреты")
    except Exception:
        log.exception("Прогрев моделей не удался — RAG будет медленным")


async def search(query: str, top_k: int = TOP_K) -> list[dict[str, Any]]:
    """Гибридный поиск: векторы + BM25, затем пересортировка реранкером."""
    import asyncio

    return await asyncio.to_thread(_search_sync, query, top_k)


def _search_sync(query: str, top_k: int) -> list[dict[str, Any]]:
    from qdrant_client import models

    client = _qdrant()
    if not client.collection_exists(COLLECTION):
        return []

    dense_vec = _dense_model().encode([query])[0].tolist()

    # Prefetch по обоим индексам, слияние — Reciprocal Rank Fusion: она
    # объединяет два ранжирования, не требуя сравнивать несравнимые оценки
    # косинуса и BM25 напрямую.
    found = client.query_points(
        COLLECTION,
        prefetch=[
            models.Prefetch(query=dense_vec, using="dense", limit=CANDIDATES),
            models.Prefetch(query=_to_sparse(query), using="bm25",
                            limit=CANDIDATES),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=CANDIDATES,
        with_payload=True,
    ).points

    if not found:
        return []

    # Реранкер включаем только на большой базе. Замеры на этом сервере:
    # 8 пар — 9 секунд, а гибридный поиск и без него ставит нужный фрагмент
    # первым при десятках документов. Порог RRF-оценки отсекает мусор.
    if not RERANK or len(found) <= top_k:
        return [{**p.payload, "score": round(float(p.score or 0), 3)}
                for p in found[:top_k]]

    pairs = [(query, p.payload.get("text", "")) for p in found]
    scores = _rerank_model().predict(pairs)

    ranked = sorted(zip(found, scores), key=lambda x: float(x[1]), reverse=True)
    out = []
    for point, score in ranked[:top_k]:
        if float(score) < MIN_SCORE:
            continue
        out.append({**point.payload, "score": round(float(score), 3)})
    return out


def as_context(results: list[dict[str, Any]]) -> str:
    """Фрагменты блоком для промпта."""
    if not results:
        return ""
    lines = ["Выдержки из научных источников (используй их для ответа "
             "и ссылайся на название источника):"]
    for r in results:
        title = r.get("title") or r.get("source") or "источник"
        lines.append(f"- [{title}] {r.get('text', '')}")
    return "\n".join(lines)


def stats() -> dict[str, Any]:
    """Сколько фрагментов в базе — для «Состояния системы»."""
    client = _qdrant()
    if not client.collection_exists(COLLECTION):
        return {"exists": False, "points": 0}
    info = client.get_collection(COLLECTION)
    return {"exists": True, "points": info.points_count or 0}
