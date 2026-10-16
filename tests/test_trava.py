from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from controle_financeiro import db as banco, servicos
from controle_financeiro.dinheiro import competencia
from controle_financeiro.main import app
from controle_financeiro.models import Lancamento, Parcelada

OUT, NOV = competencia(2026, 10), competencia(2026, 11)


@pytest.fixture
def cliente(monkeypatch):
    banco.iniciar("sqlite://")
    monkeypatch.setattr(servicos, "hoje", lambda: date(2026, 10, 15))
    with TestClient(app) as c:
        c.post("/login", data={"login": "admin", "senha": "admin"})
        c.post("/contas", data={"id": 2, "nome": "Nubank", "tipo": "cartao", "paga_por_id": 1, "ordem": 2,
                                "ativa": "1", "confirmacao": "1", "dia_fechamento": "3"})
        c.get("/mes/2026/10")
        c.get("/mes/2026/11")
        yield c


def lancar(c, comp, descricao, conta=2):
    return c.post("/lancamentos", data={"comp": comp, "conta_id": conta, "descricao": descricao, "valor": "-10"},
                  headers={"referer": "http://testserver/mes/2026/10"}, follow_redirects=False)


def descricoes(comp, conta=2):
    with banco.SessionLocal() as db:
        return {l.descricao for l in db.scalars(select(Lancamento).where(Lancamento.comp == comp,
                                                                          Lancamento.conta_id == conta))}


def test_mes_fechado_trava_cartao(cliente):
    r = lancar(cliente, OUT, "No mês errado")
    assert "erro=" in r.headers["location"] and "travada" in r.headers["location"]
    assert descricoes(OUT) == set()

    assert "erro=" not in lancar(cliente, NOV, "Certo").headers["location"]
    assert "erro=" not in lancar(cliente, OUT, "Conta corrente não trava", conta=1).headers["location"]

    r = cliente.get("/mes/2026/10")
    assert "Fatura travada — fechou em 03/10" in r.text and "Destravar para corrigir" in r.text
    r = cliente.get("/mes/2026/11")
    assert "Aberta — fecha em 03/11" in r.text


def test_nao_move_nem_edita_nem_exclui_em_mes_travado(cliente):
    lancar(cliente, NOV, "Compra")
    with banco.SessionLocal() as db:
        lid = db.scalar(select(Lancamento.id).where(Lancamento.descricao == "Compra"))
    r = cliente.post(f"/lancamentos/{lid}", data={"descricao": "Compra", "valor": "-10", "conta_id": 2, "mes": "2026-10"},
                     headers={"referer": f"http://testserver/lancamentos/{lid}"}, follow_redirects=False)
    assert "erro=" in r.headers["location"]
    assert descricoes(NOV) == {"Compra"}

    # destrava outubro, move, trava de novo: não dá mais para excluir
    cliente.post(f"/faturas/2/{OUT}/trava")
    cliente.post(f"/lancamentos/{lid}", data={"descricao": "Compra", "valor": "-10", "conta_id": 2, "mes": "2026-10"})
    assert descricoes(OUT) == {"Compra"}
    cliente.post(f"/faturas/2/{OUT}/trava")
    r = cliente.post(f"/lancamentos/{lid}/excluir", headers={"referer": "http://testserver/mes/2026/10"},
                     follow_redirects=False)
    assert "erro=" in r.headers["location"] and descricoes(OUT) == {"Compra"}

    # ✓ continua permitido
    assert cliente.post(f"/lancamentos/{lid}/confirmar").status_code == 200
    # a edição mostra o aviso e desabilita salvar
    assert "está travada" in cliente.get(f"/lancamentos/{lid}").text


def test_destravar_vale_ate_o_fim_do_dia(cliente, monkeypatch):
    r = cliente.post(f"/faturas/2/{OUT}/trava", follow_redirects=False)
    assert "destravada%20at%C3%A9%20o%20fim%20do%20dia" in r.headers["location"]
    assert "erro=" not in lancar(cliente, OUT, "Correção").headers["location"]
    assert "Destravada até o fim do dia" in cliente.get("/mes/2026/10").text

    monkeypatch.setattr(servicos, "hoje", lambda: date(2026, 10, 16))
    assert "erro=" in lancar(cliente, OUT, "Dia seguinte").headers["location"]


def test_travar_manual_mes_aberto(cliente):
    cliente.post(f"/faturas/2/{NOV}/trava")
    assert "erro=" in lancar(cliente, NOV, "x").headers["location"]
    assert "🔒 Fatura travada" in cliente.get("/mes/2026/11").text
    cliente.post(f"/faturas/2/{NOV}/trava")  # destrava
    assert "erro=" not in lancar(cliente, NOV, "x").headers["location"]


def test_cartao_sem_fechamento_so_trava_manual(cliente):
    cliente.post("/contas", data={"id": 2, "nome": "Nubank", "tipo": "cartao", "paga_por_id": 1, "ordem": 2,
                                  "ativa": "1", "dia_fechamento": ""})
    assert "erro=" not in lancar(cliente, OUT, "Livre").headers["location"]


def test_parcelado_preserva_mes_travado_e_recusa_parcela_nova(cliente):
    with banco.SessionLocal() as db:
        servicos.alternar_trava(db, db.get(servicos.Conta, 2), OUT)  # destrava para criar
        db.commit()
    cliente.post("/parceladas", data={"descricao": "TV", "conta_id": 2, "valor": "300", "parcelas": 3,
                                      "mes": "2026-10", "parcela_no_mes": 1})
    with banco.SessionLocal() as db:
        db.query(servicos.TravaFatura).delete()
        db.commit()

    r = cliente.post("/parceladas/1", data={"descricao": "TV", "conta_id": 2, "valor": "600", "parcelas": 3,
                                            "mes": "2026-10", "parcela_no_mes": 1}, follow_redirects=False)
    assert "Parcela%28s%29%201%20em%20fatura%20travada" in r.headers["location"]
    with banco.SessionLocal() as db:
        valores = {l.parcela: l.valor for l in db.scalars(select(Lancamento).where(Lancamento.parcelada_id == 1))}
    assert valores == {1: -10000, 2: -20000, 3: -20000}

    r = cliente.post("/parceladas/1/excluir", headers={"referer": "http://testserver/parceladas/1"},
                     follow_redirects=False)
    assert "erro=" in r.headers["location"]

    r = cliente.post("/parceladas", data={"descricao": "Nova", "conta_id": 2, "valor": "100", "parcelas": 2,
                                          "mes": "2026-10", "parcela_no_mes": 1},
                     headers={"referer": "http://testserver/parceladas"}, follow_redirects=False)
    assert "erro=" in r.headers["location"]
    with banco.SessionLocal() as db:
        assert db.scalar(select(Parcelada).where(Parcelada.descricao == "Nova")) is None


def test_fixo_no_cartao_preserva_mes_travado(cliente):
    cliente.post("/recorrentes", data={"descricao": "Streaming", "conta_id": "2", "valor": "-30", "inicio": "2026-11"})
    cliente.post(f"/faturas/2/{OUT}/trava")  # destrava outubro para o fixo começar nele
    cliente.post("/recorrentes/1", data={"descricao": "Streaming", "conta_id": "2", "valor": "-30",
                                         "inicio": "2026-10", "aplicar_de": "2026-10"})
    with banco.SessionLocal() as db:
        db.query(servicos.TravaFatura).delete()
        db.commit()

    r = cliente.post("/recorrentes/1", data={"descricao": "Streaming", "conta_id": "2", "valor": "-40",
                                             "inicio": "2026-10", "aplicar_de": "2026-10", "alterar_valor": "1"},
                     follow_redirects=False)
    assert "travada%20n%C3%A3o%20foram%20alterados" in r.headers["location"]
    with banco.SessionLocal() as db:
        valores = {l.comp: l.valor for l in db.scalars(select(Lancamento).where(Lancamento.recorrente_id == 1))}
    assert valores == {OUT: -3000, NOV: -4000}

    r = cliente.post("/recorrentes/1/excluir", headers={"referer": "http://testserver/recorrentes"},
                     follow_redirects=False)
    assert "erro=" in r.headers["location"]


def test_dia_de_fechamento_invalido(cliente):
    r = cliente.post("/contas", data={"id": 2, "nome": "Nubank", "tipo": "cartao", "dia_fechamento": "40"},
                     headers={"referer": "http://testserver/contas"}, follow_redirects=False)
    assert "erro=" in r.headers["location"]
    assert 'name="dia_fechamento"' in cliente.get("/contas").text
