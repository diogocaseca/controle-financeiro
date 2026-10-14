import pytest
from fastapi.testclient import TestClient

from controle_financeiro import config, db as banco
from controle_financeiro.main import app

INGRESS = {"X-Ingress-Path": "/api/hassio_ingress/abc"}


@pytest.fixture
def cliente(monkeypatch):
    banco.iniciar("sqlite://")
    monkeypatch.setattr(config, "INGRESS_PROXY_IP", "testclient")
    with TestClient(app) as c:
        yield c


def test_login_dispensado_pelo_ingress(cliente, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_VIA_INGRESS", False)
    r = cliente.get("/mes/2026/10", headers=INGRESS, follow_redirects=False)
    assert r.status_code == 200
    assert "Sair" not in r.text
    assert 'href="/api/hassio_ingress/abc/recorrentes"' in r.text


def test_sem_ingress_continua_exigindo_login(cliente, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_VIA_INGRESS", False)
    r = cliente.get("/mes/2026/10", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_ingress_de_outro_ip_exige_login(cliente, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_VIA_INGRESS", False)
    monkeypatch.setattr(config, "INGRESS_PROXY_IP", "172.30.32.2")
    r = cliente.get("/mes/2026/10", headers=INGRESS, follow_redirects=False)
    assert r.status_code == 303


def test_login_exigido_por_padrao(cliente):
    r = cliente.get("/mes/2026/10", headers=INGRESS, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/api/hassio_ingress/abc/login"
