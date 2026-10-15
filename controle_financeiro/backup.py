"""Backups do banco SQLite: automático diário, manual, download e restauração.

Os arquivos ficam em DATA_DIR/backups com o nome
financas_AAAAMMDD-HHMMSS_<tipo>.db, onde tipo é "auto", "manual" ou "restauracao"
(cópia do estado atual feita antes de restaurar outro backup).
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config, db as banco
from .models import Base

TIPOS = {"auto": "Automático", "manual": "Manual", "restauracao": "Antes de restaurar"}
_NOME = re.compile(r"^financas_(\d{8}-\d{6})(?:-\d+)?_(auto|manual|restauracao)\.db$")
_TABELAS_OBRIGATORIAS = {"usuarios", "contas", "lancamentos", "recorrentes"}


class BackupInvalido(ValueError):
    pass


@dataclass
class Backup:
    nome: str
    quando: datetime
    tipo: str
    tamanho: int

    @property
    def tipo_nome(self) -> str:
        return TIPOS[self.tipo]


def pasta() -> Path:
    p = config.DATA_DIR / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def listar() -> list[Backup]:
    itens = []
    for arq in pasta().iterdir():
        m = _NOME.match(arq.name)
        if m:
            quando = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
            itens.append(Backup(arq.name, quando, m.group(2), arq.stat().st_size))
    return sorted(itens, key=lambda b: b.quando, reverse=True)


def caminho(nome: str) -> Path:
    """Caminho de um backup existente; recusa nomes fora do padrão (ex.: '../')."""
    if not _NOME.match(nome):
        raise BackupInvalido("backup não encontrado")
    arq = pasta() / nome
    if not arq.is_file():
        raise BackupInvalido("backup não encontrado")
    return arq


def criar(tipo: str = "manual") -> Backup:
    agora = datetime.now()
    nome = f"financas_{agora:%Y%m%d-%H%M%S}_{tipo}.db"
    destino = pasta() / nome
    # Mesmo segundo (ex.: backup "antes de restaurar" logo após um manual): evita sobrescrever.
    n = 1
    while destino.exists():
        n += 1
        destino = pasta() / nome.replace(f"_{tipo}.db", f"-{n}_{tipo}.db")
    bruta = banco.engine.raw_connection()
    try:
        alvo = sqlite3.connect(destino)
        with alvo:
            bruta.driver_connection.backup(alvo)
        alvo.close()
    finally:
        bruta.close()
    return Backup(destino.name, agora.replace(microsecond=0), tipo, destino.stat().st_size)


def diario() -> Backup | None:
    """Cria o backup automático do dia, se ainda não existir, e apaga os antigos."""
    hoje = datetime.now().date()
    if any(b.tipo == "auto" and b.quando.date() == hoje for b in listar()):
        return None
    novo = criar("auto")
    limpar()
    return novo


def limpar() -> None:
    """Mantém os N automáticos e os 10 "antes de restaurar" mais recentes; manuais ficam."""
    limites = {"auto": config.BACKUPS_MANTER, "restauracao": 10}
    por_tipo: dict[str, int] = {}
    for b in listar():
        por_tipo[b.tipo] = por_tipo.get(b.tipo, 0) + 1
        if b.tipo in limites and por_tipo[b.tipo] > limites[b.tipo]:
            (pasta() / b.nome).unlink(missing_ok=True)


def _validar(arq: Path) -> None:
    try:
        con = sqlite3.connect(f"file:{arq}?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupInvalido("o arquivo de backup está corrompido")
            tabelas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        raise BackupInvalido("o arquivo não é um banco SQLite válido") from e
    if not _TABELAS_OBRIGATORIAS <= tabelas:
        raise BackupInvalido("o arquivo não é um backup do Controle Financeiro")


def restaurar(nome: str) -> Backup:
    """Substitui o banco atual pelo backup. Antes, guarda o estado atual."""
    arq = caminho(nome)
    _validar(arq)
    seguranca = criar("restauracao")
    origem = sqlite3.connect(f"file:{arq}?mode=ro", uri=True)
    bruta = banco.engine.raw_connection()
    try:
        origem.backup(bruta.driver_connection)
    finally:
        bruta.close()
        origem.close()
    # Backup de uma versão antiga: cria tabelas/colunas que surgiram depois.
    Base.metadata.create_all(banco.engine)
    banco._migrar(banco.engine)
    return seguranca
