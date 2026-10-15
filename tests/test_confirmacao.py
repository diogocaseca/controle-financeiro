import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from controle_financeiro import db as banco, servicos
from controle_financeiro.dinheiro import competencia
from controle_financeiro.main import app
from controle_financeiro.models import Lancamento, Parcelada, Recorrente

OUT = competencia(2026, 10)


@pytest.fixture
def cliente():
    banco.iniciar("sqlite://")
    with TestClient(app) as c:
        c.post("/login", data={"login": "admin", "senha": "admin"})
        c.get("/mes/2026/10")
        yield c


def test_marcar_e_desmarcar_lancamento(cliente):
    cliente.post("/lancamentos", data={"comp": OUT, "conta_id": 1, "descricao": "Salário", "valor": "10000"})
    cliente.post("/lancamentos", data={"comp": OUT, "conta_id": 1, "descricao": "Light", "valor": "-200"})

    r = cliente.get("/mes/2026/10")
    assert "2 itens sem ✓" in r.text

    r = cliente.post("/lancamentos/1/confirmar")
    assert r.status_code == 200 and "1 item sem ✓" in r.text
    with banco.SessionLocal() as db:
        assert db.get(Lancamento, 1).confirmado

    cliente.post("/lancamentos/1/confirmar")
    with banco.SessionLocal() as db:
        assert not db.get(Lancamento, 1).confirmado


def test_marcar_fatura(cliente):
    cliente.post("/lancamentos", data={"comp": OUT, "conta_id": 2, "descricao": "Compra", "valor": "-50"})
    r = cliente.post(f"/faturas/2/{OUT}/confirmar")
    assert "Tudo ✓" in r.text
    with banco.SessionLocal() as db:
        assert servicos.resumo_mes(db, OUT).contas[0].cartoes[0].confirmada
    r = cliente.post(f"/faturas/2/{OUT}/confirmar")
    assert "1 item sem ✓" in r.text


def test_editar_fixo_mantem_confirmacao(cliente):
    with banco.SessionLocal() as db:
        r = Recorrente(descricao="TIM", conta_id=1, valor=-7849, inicio=OUT)
        db.add(r)
        db.flush()
        servicos.sincronizar_recorrente(db, r, OUT)
        db.commit()
        l = db.scalar(select(Lancamento).where(Lancamento.recorrente_id == r.id))
        l.confirmado = True
        r.valor = -8000
        servicos.sincronizar_recorrente(db, r, OUT)
        db.commit()
        l = db.scalar(select(Lancamento).where(Lancamento.recorrente_id == r.id))
        assert (l.valor, l.confirmado) == (-8000, True)


def test_editar_parcelada_mantem_confirmacao(cliente):
    with banco.SessionLocal() as db:
        p = Parcelada(descricao="TV", conta_id=2, valor_total=-30000, parcelas=3, primeira=OUT, lancada_desde=1)
        db.add(p)
        db.flush()
        servicos.gerar_parcelas(db, p)
        db.commit()
        primeira = db.scalar(select(Lancamento).where(Lancamento.parcelada_id == p.id, Lancamento.parcela == 1))
        primeira.confirmado = True
        servicos.gerar_parcelas(db, p)
        db.commit()
        ok = {l.parcela: l.confirmado for l in db.scalars(select(Lancamento).where(Lancamento.parcelada_id == p.id))}
        assert ok == {1: True, 2: False, 3: False}


def test_migra_banco_antigo(tmp_path):
    arq = tmp_path / "antigo.db"
    con = sqlite3.connect(arq)
    con.execute("CREATE TABLE lancamentos (id INTEGER PRIMARY KEY, conta_id INTEGER, comp INTEGER, descricao TEXT,"
                " valor INTEGER, valor_expr TEXT, recorrente_id INTEGER, parcelada_id INTEGER, parcela INTEGER,"
                " verba_id INTEGER, eh_verba BOOLEAN, ordem INTEGER)")
    con.execute("INSERT INTO lancamentos (conta_id, comp, descricao, valor) VALUES (1, 24321, 'Antigo', -100)")
    con.commit()
    con.close()

    banco.iniciar(f"sqlite:///{arq}")
    with banco.SessionLocal() as db:
        l = db.scalar(select(Lancamento))
        assert l.descricao == "Antigo" and l.confirmado is False


def test_marcar_pelo_ingress(cliente):
    cliente.post("/lancamentos", data={"comp": OUT, "conta_id": 1, "descricao": "Light", "valor": "-200"})
    r = cliente.post("/lancamentos/1/confirmar", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    assert r.status_code == 200 and "Tudo ✓" in r.text


def test_desligar_marcacao_na_conta(cliente):
    cliente.post("/lancamentos", data={"comp": OUT, "conta_id": 1, "descricao": "Light", "valor": "-200"})
    cliente.post("/lancamentos", data={"comp": OUT, "conta_id": 2, "descricao": "Compra", "valor": "-50"})
    r = cliente.get("/mes/2026/10")
    assert "Marcar como confirmado: Compra" in r.text
    assert "2 itens sem ✓" in r.text  # Light + linha da fatura

    # Cartão sem marcação: some o ✓ das compras e do total, mas a linha da fatura na Nuconta continua
    cliente.post("/contas", data={"id": 2, "nome": "Nubank", "tipo": "cartao", "paga_por_id": 1, "ordem": 2, "ativa": "1"})
    r = cliente.get("/mes/2026/10")
    assert "Marcar como confirmado: Compra" not in r.text
    assert r.text.count("Marcar como confirmado: fatura Nubank") == 1
    assert "2 itens sem ✓" in r.text

    # Conta corrente sem marcação: nada para confirmar no mês
    cliente.post("/contas", data={"id": 1, "nome": "Nuconta", "tipo": "corrente", "ordem": 1, "ativa": "1"})
    r = cliente.get("/mes/2026/10")
    assert "Marcar como confirmado" not in r.text
    assert "A confirmar" not in r.text

    # Religando
    cliente.post("/contas", data={"id": 1, "nome": "Nuconta", "tipo": "corrente", "ordem": 1, "ativa": "1", "confirmacao": "1"})
    assert "A confirmar" in cliente.get("/mes/2026/10").text


def test_conta_nova_vem_com_marcacao(cliente):
    cliente.post("/contas", data={"nome": "Itaú", "tipo": "corrente", "ordem": 3, "confirmacao": "1"})
    r = cliente.get("/contas")
    assert r.text.count('name="confirmacao" value="1" checked') == 4  # 3 contas + formulário de nova
