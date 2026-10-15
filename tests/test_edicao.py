import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from controle_financeiro import db as banco
from controle_financeiro.main import app
from controle_financeiro.models import Lancamento, Movimento


@pytest.fixture
def cliente():
    banco.iniciar("sqlite://")
    with TestClient(app) as c:
        c.post("/login", data={"login": "admin", "senha": "admin"})
        yield c


def test_editar_movimento(cliente):
    cliente.post("/controles", data={"nome": "Fabiane"})
    cliente.post("/controles", data={"nome": "Pai", "grupo": "poupanca"})
    cliente.post("/controles/1/movimentos", data={"mes": "2026-10", "descricao": "Errado", "valor": "-50"})

    r = cliente.get("/controles/1")
    assert 'href="/movimentos/1"' in r.text
    assert cliente.get("/movimentos/1").status_code == 200

    r = cliente.post("/movimentos/1", data={
        "descricao": "Corrigido", "valor": "-156,98/2", "controle_id": "2", "mes": "2026-11",
    }, follow_redirects=False)
    assert r.headers["location"].startswith("/controles/1")

    with banco.SessionLocal() as db:
        m = db.get(Movimento, 1)
        assert (m.descricao, m.valor, m.controle_id, m.valor_expr) == ("Corrigido", -7849, 2, "-156,98/2")


def test_editar_lancamento_pelo_mes(cliente):
    r = cliente.get("/mes/2026/10")
    cliente.post("/lancamentos", data={"comp": 24321, "conta_id": 1, "descricao": "Mercdo", "valor": "-10"})
    r = cliente.get("/mes/2026/10")
    assert 'title="Editar"' in r.text

    with banco.SessionLocal() as db:
        lid = db.scalar(select(Lancamento.id).where(Lancamento.descricao == "Mercdo"))
    cliente.post(f"/lancamentos/{lid}", data={"descricao": "Mercado", "valor": "-100", "conta_id": 1, "mes": "2026-10"})
    with banco.SessionLocal() as db:
        l = db.get(Lancamento, lid)
        assert (l.descricao, l.valor) == ("Mercado", -10000)
