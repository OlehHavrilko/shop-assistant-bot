import asyncio
import importlib.util
import json
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from shop_assistant.catalog import Catalog
from shop_assistant.importer import import_catalog
from shop_assistant.limits import Budget, Tariffs
from shop_assistant.llm import LLM, SearchDecision


def response(name="search_catalog", params=None, *, stop="tool_use"):
    return NS(stop_reason=stop, usage=NS(input_tokens=100, output_tokens=50),
              content=[NS(type="tool_use", id="call1", name=name, input=params or {"name": "Льон"}),
                       NS(type="text", text="Invented price: 0.01; supplier: guessed")])


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.messages = self

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def setup(tmp_path):
    catalog = Catalog(tmp_path / "catalog.db")
    import_catalog(catalog, "examples/catalog.csv")
    budget = Budget(tmp_path / "budget.db", Decimal("1"),
                    Tariffs(Decimal("1"), Decimal("5"), Decimal("2"), Decimal("0.1")))
    yield catalog, budget
    catalog.close()


def run(setup, client, **kwargs):
    catalog, budget = setup
    llm = LLM(catalog, model="test-model", budget=budget, client=client, **kwargs)
    return asyncio.run(llm.resolve("Потрібен льон ivory"))


def test_tool_results_only_public_data_and_no_model_prose(setup):
    client = FakeClient(response(params={"sku": "00123"}),
                        response("choose_products", {"action": "products", "skus": ["00123"]}))
    assert run(setup, client) == SearchDecision("products", ("00123",))
    payload = json.dumps(client.requests[1], ensure_ascii=False)
    assert "purchase_price" not in payload and "supplier" not in payload
    assert "345.50" in payload and "Invented price" not in payload
    assert setup[0].search(sku="00123")[0].price_uah_per_m == Decimal("345.50")
    assert len(client.requests) == 2


@pytest.mark.parametrize("params", [
    {"action": "products", "skus": ["invented"]},
    {"action": "products", "skus": ["00123"], "price": "0.01"},
    {"action": [], "skus": []},
    {"action": "products", "skus": "00123"},
])
def test_invalid_identifiers_and_malformed_decisions_fallback(setup, params):
    client = FakeClient(response(), response("choose_products", params))
    assert run(setup, client).action == "handoff"


@pytest.mark.parametrize("params", [{"supplier": "secret"}, {"sku": 123},
    {"name": "x" * 201}, {"sku": "00123", "shade": "ivory"}])
def test_invalid_search_arguments_fallback(setup, params):
    client = FakeClient(response(params=params))
    assert run(setup, client).action == "handoff"
    assert len(client.requests) == 1


def test_unknown_sku_no_invented_products(setup):
    client = FakeClient(response(params={"sku": "invented"}))
    assert run(setup, client) == SearchDecision("not_found")


def test_timeout_retains_maximum_reservation(setup):
    assert run(setup, FakeClient(TimeoutError())).action == "handoff"
    assert setup[1].spent() == Decimal("0.034")


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "end_turn"])
def test_refusal_truncation_and_prose_fallback(setup, stop):
    assert run(setup, FakeClient(response(stop=stop))).action == "handoff"


def test_input_bound_and_budget_prevent_api_calls(setup):
    client = FakeClient()
    assert run(setup, client, max_input_tokens=1).action == "handoff"
    assert client.requests == []
    while setup[1].reserve(16000, 400):
        pass
    assert run(setup, client).action == "handoff"
    assert client.requests == []


def test_no_key_never_creates_client(setup):
    assert run(setup, None).action == "clarify"


def test_single_call_returns_ambiguous_candidates(setup):
    client = FakeClient(response(params={"name": "Льон"}))
    result = run(setup, client, max_calls=1)
    assert result.action == "products" and len(result.skus) >= 2
    assert len(client.requests) == 1


def test_unsupported_question_can_handoff_without_fake_search(setup):
    client = FakeClient(response("choose_products", {"action": "handoff", "skus": []}))
    assert run(setup, client).action == "handoff"
    assert len(client.requests) == 1


def test_first_call_cannot_select_unsearched_products(setup):
    client = FakeClient(response("choose_products", {"action": "products", "skus": ["00123"]}))
    assert run(setup, client).action == "handoff"
    assert len(client.requests) == 1


def test_unresolved_shade_variants_cannot_be_silently_selected(setup):
    catalog, _ = setup
    with catalog.connection:
        catalog.connection.execute("UPDATE products SET name='Льон Siena' WHERE sku='123'")
    client = FakeClient(response(params={"name": "Льон Siena"}),
                        response("choose_products", {"action": "products", "skus": ["00123"]}))
    assert set(run(setup, client).skus) == {"00123", "123", "LIN-002"}


@pytest.mark.parametrize("action", ["products", "clarify"])
def test_partial_shade_keeps_matching_same_fabric_variants(setup, action):
    client = FakeClient(response(params={"name": "Льон Siena", "shade": "ivory"}),
                        response("choose_products", {"action": action, "skus": ["00123"]}))
    decision = run(setup, client)
    assert decision.action == action
    assert set(decision.skus) == {"00123", "LIN-002"}


@pytest.mark.parametrize("action", ["not_found", "handoff"])
def test_nonproduct_actions_reject_identifiers(setup, action):
    client = FakeClient(response(params={"sku": "00123"}),
                        response("choose_products", {"action": action, "skus": ["00123"]}))
    assert run(setup, client) == SearchDecision("handoff")


@pytest.mark.skipif(importlib.util.find_spec("anthropic") is None, reason="needs the optional .[llm] extra")
def test_official_sdk_constructor_offline(setup):
    catalog, budget = setup
    async def check():
        llm = LLM(catalog, model="offline-constructor-test", budget=budget,
                  api_key="fake-key-no-network-call")
        assert llm.client.max_retries == 0
        assert llm.client.timeout == 15.0
        await llm.close()
    asyncio.run(check())


def test_removed_identity_after_model_response_not_returned(setup):
    class ChangingClient(FakeClient):
        async def create(self, **kwargs):
            result = await super().create(**kwargs)
            if len(self.requests) == 2:
                with setup[0].connection:
                    setup[0].connection.execute("DELETE FROM products WHERE sku='00123'")
            return result
    client = ChangingClient(response(params={"sku": "00123"}),
                            response("choose_products", {"action": "products", "skus": ["00123"]}))
    assert run(setup, client).action == "handoff"
