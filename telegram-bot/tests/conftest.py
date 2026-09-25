"""Nessun test parla con la rete: il portale CUP vero non va mai toccato dai test."""
import pytest
import requests


@pytest.fixture(autouse=True)
def niente_rete(monkeypatch):
    def vietato(*a, **k):
        raise AssertionError("richiesta di rete in un test")
    monkeypatch.setattr(requests.Session, "request", vietato)
