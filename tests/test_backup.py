import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from controle_financeiro import backup, config, db as banco
from controle_financeiro.main import app
from controle_financeiro.models import Lancamento


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    banco.iniciar(f"sqlite:///{tmp_path / 'financas.db'}")
    with TestClient(app) as c:
        c.post("/login", data={"login": "admin", "senha": "admin"})
        yield c


def lancar(c, descricao):
    c.post("/lancamentos", data={"comp": 24321, "conta_id": 1, "descricao": descricao, "valor": "-10"})


def descricoes():
    with banco.SessionLocal() as db:
        return {l.descricao for l in db.scalars(select(Lancamento))}


def test_backup_manual_listar_e_baixar(cliente):
    lancar(cliente, "Antes")
    r = cliente.post("/backups", follow_redirects=False)
    assert "Backup%20criado" in r.headers["location"]

    [b] = backup.listar()
    assert b.tipo == "manual"
    r = cliente.get("/backups")
    assert b.nome in r.text and "Restaurar" in r.text

    r = cliente.get(f"/backups/{b.nome}")
    assert r.status_code == 200
    assert b.nome in r.headers["content-disposition"]
    assert r.content[:16] == b"SQLite format 3\x00"


def test_restaurar_guarda_estado_atual(cliente):
    lancar(cliente, "Antes")
    cliente.post("/backups")
    [b] = backup.listar()
    lancar(cliente, "Depois")
    assert descricoes() == {"Antes", "Depois"}

    r = cliente.post(f"/backups/{b.nome}/restaurar", follow_redirects=False)
    assert "restaurado" in r.headers["location"]
    assert descricoes() == {"Antes"}

    seguranca = next(x for x in backup.listar() if x.tipo == "restauracao")
    cliente.post(f"/backups/{seguranca.nome}/restaurar")
    assert descricoes() == {"Antes", "Depois"}  # desfez


def test_restaurar_backup_de_versao_antiga_migra(cliente, tmp_path):
    antigo = backup.pasta() / "financas_20260101-000000_manual.db"
    con = sqlite3.connect(antigo)
    con.executescript("""
        CREATE TABLE usuarios (id INTEGER PRIMARY KEY, login TEXT, senha_hash TEXT, senha_padrao BOOLEAN);
        CREATE TABLE contas (id INTEGER PRIMARY KEY, nome TEXT, tipo TEXT, paga_por_id INTEGER, ordem INTEGER, ativa BOOLEAN);
        CREATE TABLE recorrentes (id INTEGER PRIMARY KEY, descricao TEXT);
        CREATE TABLE lancamentos (id INTEGER PRIMARY KEY, conta_id INTEGER, comp INTEGER, descricao TEXT, valor INTEGER,
            valor_expr TEXT, recorrente_id INTEGER, parcelada_id INTEGER, parcela INTEGER, verba_id INTEGER,
            eh_verba BOOLEAN, ordem INTEGER);
        INSERT INTO contas VALUES (1, 'Nuconta', 'corrente', NULL, 1, 1);
        INSERT INTO lancamentos (conta_id, comp, descricao, valor) VALUES (1, 24321, 'Antigo', -100);
    """)
    con.commit()
    con.close()

    cliente.post("/backups/financas_20260101-000000_manual.db/restaurar")
    assert descricoes() == {"Antigo"}
    assert cliente.get("/mes/2026/10").status_code in (200, 303)


def test_recusa_arquivos_invalidos(cliente):
    r = cliente.get("/backups/..%2Ffinancas.db", follow_redirects=False)
    assert r.status_code in (303, 404)
    (backup.pasta() / "financas_20260101-000000_manual.db").write_text("lixo")
    r = cliente.post("/backups/financas_20260101-000000_manual.db/restaurar", follow_redirects=False)
    assert "erro=" in r.headers["location"]


def test_diario_um_por_dia_e_limpeza(cliente, monkeypatch):
    assert backup.diario() is not None
    assert backup.diario() is None  # já existe o de hoje

    monkeypatch.setattr(config, "BACKUPS_MANTER", 3)
    for i in range(5):
        (backup.pasta() / f"financas_2025010{i + 1}-000000_auto.db").write_bytes(b"")
    (backup.pasta() / "financas_20240101-000000_manual.db").write_bytes(b"")
    backup.limpar()
    tipos = [b.tipo for b in backup.listar()]
    assert tipos.count("auto") == 3 and tipos.count("manual") == 1


def test_quantidade_configuravel_pela_tela(cliente):
    assert backup.manter() == config.BACKUPS_MANTER
    for i in range(5):
        (backup.pasta() / f"financas_2025010{i + 1}-000000_auto.db").write_bytes(b"")
    (backup.pasta() / "financas_20240101-000000_manual.db").write_bytes(b"")

    r = cliente.get("/backups")
    assert 'name="manter"' in r.text and "Hoje há 5 automático(s)" in r.text

    r = cliente.post("/backups/configuracao", data={"manter": 2}, follow_redirects=False)
    assert "3%20backup" in r.headers["location"]  # apagou os 3 mais antigos
    assert backup.manter() == 2
    nomes = [b.nome for b in backup.listar()]
    assert "financas_20250105-000000_auto.db" in nomes and "financas_20250101-000000_auto.db" not in nomes
    assert "financas_20240101-000000_manual.db" in nomes

    # o automático diário respeita o novo limite
    backup.diario()
    assert sum(b.tipo == "auto" for b in backup.listar()) == 2

    r = cliente.post("/backups/configuracao", data={"manter": 0}, follow_redirects=False)
    assert "erro=" in r.headers["location"] and backup.manter() == 2
