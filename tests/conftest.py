import pytest

from shop_assistant.storefront import Storefront


def pytest_configure(config):
    config.addinivalue_line('markers', 'hours: use the real working-hours check instead of an always-open shop')


@pytest.fixture(autouse=True)
def always_open(request, monkeypatch):
    # The demo store.json has working hours; most tests must not depend on the wall clock.
    if 'hours' not in request.keywords:
        monkeypatch.setattr(Storefront, 'is_open', lambda self, now: True)
