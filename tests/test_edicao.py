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


def test_parcelada_com_verba_pela_tela(cliente):
    cliente.get("/mes/2026/10")
    cliente.post("/recorrentes", data={"descricao": "Limpeza", "conta_id": "1", "valor": "-600", "verba": "1", "inicio": "2026-10"})
    r = cliente.get("/parceladas")
    assert 'name="verba_valor"' in r.text and ">Limpeza</option>" in r.text

    cliente.post("/parceladas", data={
        "descricao": "Lava louça", "conta_id": 2, "valor": "1951,30", "tipo_valor": "total", "parcelas": 10,
        "mes": "2026-10", "parcela_no_mes": 1, "verba_id": "1", "verba_valor": "195,13",
    })
    r = cliente.get("/mes/2026/10")
    assert "-404,87" in r.text  # -600 + 195,13

    # Ajuste numa parcela isolada pela edição do lançamento
    with banco.SessionLocal() as db:
        lid = db.scalar(select(Lancamento.id).where(Lancamento.parcela == 1))
    cliente.post(f"/lancamentos/{lid}", data={
        "descricao": "Lava louça", "valor": "-195,13", "conta_id": 2, "verba_id": "1", "verba_valor": "100",
    })
    r = cliente.get("/mes/2026/10")
    assert "-500,00" in r.text and "desconta 100,00 da Limpeza" in r.text


def test_excluir_fixo(cliente):
    from controle_financeiro.models import Movimento, Parcelada, Recorrente

    cliente.get("/mes/2026/10")
    cliente.get("/mes/2026/11")
    cliente.post("/controles", data={"nome": "Fabiane"})
    cliente.post("/recorrentes", data={"descricao": "TIM", "conta_id": "1", "valor": "-156,98/2", "inicio": "2026-10",
                                       "controle_id": "1", "controle_valor": "78,49"})
    cliente.post("/recorrentes", data={"descricao": "Limpeza", "conta_id": "1", "valor": "-600", "verba": "1", "inicio": "2026-10"})
    cliente.post("/lancamentos", data={"comp": 24321, "conta_id": 2, "descricao": "Hortifruti", "valor": "-50", "verba_id": "2"})
    cliente.post("/parceladas", data={"descricao": "Lava louça", "conta_id": 2, "valor": "100", "parcelas": 2,
                                      "mes": "2026-10", "verba_id": "2", "verba_valor": "50"})

    r = cliente.get("/recorrentes/1")
    assert "2 lançamento(s) e 2 movimento(s) em Fabiane" in r.text
    assert "/recorrentes/1/excluir" in cliente.get("/recorrentes").text

    cliente.post("/recorrentes/1/excluir")
    cliente.post("/recorrentes/2/excluir")

    with banco.SessionLocal() as db:
        assert db.scalars(select(Recorrente)).all() == []
        assert db.scalars(select(Movimento)).all() == []
        restantes = db.scalars(select(Lancamento)).all()
        assert {l.descricao for l in restantes} == {"Hortifruti", "Lava louça"}
        assert all(l.verba_id is None and l.verba_valor is None for l in restantes)
        assert db.scalar(select(Parcelada)).verba_id is None
    assert cliente.get("/mes/2026/10").status_code == 200


def test_quitadas_com_botao_esconder(cliente):
    cliente.post("/parceladas", data={"descricao": "Antiga", "conta_id": 2, "valor": "100", "parcelas": 2,
                                      "mes": "2020-01", "parcela_no_mes": 1})
    r = cliente.get("/parceladas")
    assert "Quitadas (1)" in r.text
    assert "Esconder quitadas" in r.text and "Mostrar quitadas (1)" in r.text


def test_encerrados_com_botao_esconder(cliente):
    cliente.post("/recorrentes", data={"descricao": "Antigo", "conta_id": "1", "valor": "-10",
                                       "inicio": "2020-01", "fim": "2020-06"})
    r = cliente.get("/recorrentes")
    assert "Encerrados (1)" in r.text
    assert "Esconder encerrados" in r.text and "Mostrar encerrados (1)" in r.text
    assert 'data-chave="cf.esconder.encerrados"' in r.text


def test_valor_da_parcela_nao_muda_pelo_mes(cliente):
    cliente.get("/mes/2026/10")
    cliente.post("/parceladas", data={"descricao": "TV", "conta_id": 2, "valor": "300", "parcelas": 3,
                                      "mes": "2026-10", "parcela_no_mes": 1})
    with banco.SessionLocal() as db:
        lid = db.scalar(select(Lancamento.id).where(Lancamento.parcela == 2))
    r = cliente.get(f"/lancamentos/{lid}")
    assert 'name="valor" value="-100,00" readonly' in r.text and "edite a compra" in r.text

    cliente.post(f"/lancamentos/{lid}", data={"descricao": "TV nova", "valor": "-1", "conta_id": 2})
    with banco.SessionLocal() as db:
        l = db.get(Lancamento, lid)
        assert (l.descricao, l.valor) == ("TV nova", -10000)

    # lançamento comum continua editável
    cliente.post("/lancamentos", data={"comp": 24321, "conta_id": 1, "descricao": "Avulso", "valor": "-5"})
    with banco.SessionLocal() as db:
        aid = db.scalar(select(Lancamento.id).where(Lancamento.descricao == "Avulso"))
    cliente.post(f"/lancamentos/{aid}", data={"descricao": "Avulso", "valor": "-7", "conta_id": 1})
    with banco.SessionLocal() as db:
        assert db.get(Lancamento, aid).valor == -700
