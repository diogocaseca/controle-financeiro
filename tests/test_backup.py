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


def _banco_versao_antiga(arq):
    """Esquema da 0.1.0: o atual sem as colunas/tabelas acrescentadas depois."""
    from sqlalchemy import MetaData, Table, create_engine

    from controle_financeiro.models import Base

    novas = {(t, c) for t, c, _ in banco._COLUNAS_NOVAS}
    md = MetaData()
    for t in Base.metadata.sorted_tables:
        if t.name in ("faturas_confirmadas", "configuracoes"):
            continue
        Table(t.name, md, *[c._copy() for c in t.columns if (t.name, c.name) not in novas])
    eng = create_engine(f"sqlite:///{arq}")
    md.create_all(eng)
    with eng.begin() as con:
        con.exec_driver_sql("INSERT INTO usuarios (login, senha_hash, senha_padrao) VALUES ('admin', 'x', 1)")
        con.exec_driver_sql("INSERT INTO contas (id, nome, tipo, ordem, ativa) VALUES (1, 'Nuconta', 'corrente', 1, 1)")
        con.exec_driver_sql("INSERT INTO lancamentos (conta_id, comp, descricao, valor, valor_expr, eh_verba, ordem)"
                            " VALUES (1, 24321, 'Antigo', -100, '', 0, 0)")
    eng.dispose()


def test_restaurar_backup_de_versao_antiga_migra(cliente):
    antigo = backup.pasta() / "financas_20260101-000000_manual.db"
    _banco_versao_antiga(antigo)
    colunas = {r[1] for r in sqlite3.connect(antigo).execute("PRAGMA table_info(lancamentos)")}
    assert "confirmado" not in colunas

    cliente.post("/backups/financas_20260101-000000_manual.db/restaurar")
    assert descricoes() == {"Antigo"}
    assert cliente.get("/mes/2026/10", follow_redirects=False).status_code in (200, 303)
    with banco.SessionLocal() as db:
        assert db.scalar(select(Lancamento)).confirmado is False


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


def _backup_de_outra_instalacao(tmp_path, descricao="De outra casa", com_usuario=True):
    """Gera um banco completo do sistema num arquivo separado, como se baixado de outra instalação."""
    from controle_financeiro.models import Base, Conta, Usuario

    arq = tmp_path / "outra.db"
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    eng = create_engine(f"sqlite:///{arq}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        if com_usuario:
            s.add(Usuario(login="outro", senha_hash=banco.hash_senha("segredo"), senha_padrao=False))
        s.add(Conta(id=1, nome="Itaú", tipo="corrente", ordem=1))
        s.add(Lancamento(conta_id=1, comp=24321, descricao=descricao, valor=-100))
        s.commit()
    eng.dispose()
    return arq


def test_importar_arquivo_de_outra_instalacao(cliente, tmp_path):
    lancar(cliente, "Daqui")
    arq = _backup_de_outra_instalacao(tmp_path)

    r = cliente.get("/backups")
    assert 'accept=".db"' in r.text and "Restaurar de um arquivo" in r.text

    with arq.open("rb") as f:
        r = cliente.post("/backups/importar", files={"arquivo": ("backup-casa.db", f, "application/octet-stream")},
                         follow_redirects=False)
    assert "validado%20e%20restaurado" in r.headers["location"]
    assert descricoes() == {"De outra casa"}

    tipos = [b.tipo for b in backup.listar()]
    assert "importado" in tipos and "restauracao" in tipos  # fica no histórico e dá para desfazer

    # login passou a ser o da outra instalação
    cliente.post("/logout")
    r = cliente.post("/login", data={"login": "outro", "senha": "segredo"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"

    # desfazer
    cliente.post("/login", data={"login": "outro", "senha": "segredo"})
    seguranca = next(b for b in backup.listar() if b.tipo == "restauracao")
    cliente.post(f"/backups/{seguranca.nome}/restaurar")
    assert descricoes() == {"Daqui"}


@pytest.mark.parametrize("nome,conteudo,erro", [
    ("planilha.xlsx", b"PK\x03\x04", "selecione%20um%20arquivo"),
    ("falso.db", b"isso nao e um banco", "n%C3%A3o%20%C3%A9%20um%20backup"),
    ("vazio.db", b"", "n%C3%A3o%20%C3%A9%20um%20backup"),
])
def test_importar_recusa_arquivos_invalidos(cliente, nome, conteudo, erro):
    lancar(cliente, "Daqui")
    r = cliente.post("/backups/importar", files={"arquivo": (nome, conteudo)}, follow_redirects=False)
    assert "erro=" in r.headers["location"] and erro in r.headers["location"]
    assert descricoes() == {"Daqui"}  # nada mudou
    assert backup.listar() == []  # nem entrou na lista
    assert not list(backup.pasta().glob(".envio-*"))  # temporário apagado


def test_importar_sqlite_de_outro_programa(cliente, tmp_path):
    lancar(cliente, "Daqui")
    arq = tmp_path / "outro_programa.db"
    con = sqlite3.connect(arq)
    con.execute("CREATE TABLE fotos (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    r = cliente.post("/backups/importar", files={"arquivo": ("fotos.db", arq.read_bytes())}, follow_redirects=False)
    assert "n%C3%A3o%20%C3%A9%20um%20backup%20do%20Controle%20Financeiro" in r.headers["location"]
    assert descricoes() == {"Daqui"}


def test_importar_banco_sem_usuarios_recria_admin(cliente, tmp_path):
    arq = _backup_de_outra_instalacao(tmp_path, com_usuario=False)
    cliente.post("/backups/importar", files={"arquivo": ("sem-usuario.db", arq.read_bytes())})
    cliente.post("/logout")
    r = cliente.post("/login", data={"login": "admin", "senha": "admin"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_backups_nao_deixam_arquivos_soltos(cliente, tmp_path):
    lancar(cliente, "Daqui")
    cliente.post("/backups")
    [b] = backup.listar()
    assert sqlite3.connect(backup.caminho(b.nome)).execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    arq = _backup_de_outra_instalacao(tmp_path)
    sqlite3.connect(arq).execute("PRAGMA journal_mode=WAL").close()  # como o banco de outra instalação
    cliente.post("/backups/importar", files={"arquivo": ("outra.db", arq.read_bytes())})
    cliente.post(f"/backups/{b.nome}/restaurar")

    soltos = [p.name for p in backup.pasta().iterdir() if p.name.endswith(("-wal", "-shm")) or p.name.startswith(".")]
    assert soltos == []
