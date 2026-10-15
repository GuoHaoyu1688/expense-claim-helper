import pytest
from PIL import Image

from expense_claim_helper.core.common import settings
from expense_claim_helper.core.store import Store


@pytest.fixture
def store(tmp_path):
    root, config = settings(tmp_path / "data")
    instance = Store(root, config)
    yield instance
    instance.close()


@pytest.fixture
def receipt(tmp_path):
    path = tmp_path / "fictional-receipt.png"
    Image.new("RGB", (200, 100), "white").save(path)
    return path


@pytest.fixture
def trip(store):
    return store.add_trip("2026-01-30T15:00", "2026-02-02T10:00", "CITY-A", "CITY-B", "B")
