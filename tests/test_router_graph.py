"""Граф-роутер верхнего уровня.

Классификацию делает LLM, а не регулярки: прежняя эвристика предсказуемо мазала
на «а можно мне 200 грамм риса?» — есть «мне» и «?», значит уходило в вопросы
вместо записи еды.

Здесь проверяем только детерминированную часть: структуру графа и поведение при
недоступной или мусорной модели. Качество классификации проверяется на живой
модели вручную.
"""
import pytest

from app.graph.router_graph import build_router_graph, node_router, route


class TestGraphShape:
    def test_nodes_and_edges(self):
        g = build_router_graph().get_graph()
        nodes = {n for n in g.nodes if not n.startswith("__")}
        assert nodes == {"router", "agent", "food"}

        pairs = {(e.source, e.target) for e in g.edges}
        assert ("__start__", "router") in pairs
        assert ("router", "agent") in pairs
        assert ("router", "food") in pairs

    def test_route_picks_food_only_on_food_intent(self):
        assert route({"intent": "food"}) == "food"
        assert route({"intent": "agent"}) == "agent"

    def test_unknown_intent_goes_to_agent(self):
        """Безопасный дефолт: лишний вопрос лучше лишней записи в базу."""
        assert route({}) == "agent"
        assert route({"intent": "мусор"}) == "agent"


class TestRouterFallback:
    @pytest.mark.asyncio
    async def test_empty_text_does_not_call_llm(self):
        out = await node_router({"text": "   "})
        assert out["intent"] == "agent"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_agent(self, monkeypatch):
        class Boom:
            async def complete(self, *a, **kw):
                raise RuntimeError("нет сети")

        monkeypatch.setattr("app.graph.router_graph.get_llm", lambda: Boom())
        out = await node_router({"text": "съел банан"})
        assert out["intent"] == "agent"

    @pytest.mark.asyncio
    async def test_garbage_json_falls_back_to_agent(self, monkeypatch):
        class Junk:
            async def complete(self, *a, **kw):
                return "не json вовсе"

        monkeypatch.setattr("app.graph.router_graph.get_llm", lambda: Junk())
        out = await node_router({"text": "привет"})
        assert out["intent"] == "agent"
