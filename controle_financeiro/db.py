import hashlib
import hmac
import secrets

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from . import config
from .models import Base, Conta, Usuario

engine: Engine | None = None
SessionLocal = sessionmaker(expire_on_commit=False)


def iniciar(url: str | None = None) -> Engine:
    global engine
    url = url or config.DB_URL
    if url == config.DB_URL and url.startswith("sqlite:///"):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if url in ("sqlite://", "sqlite:///:memory:"):
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SessionLocal.configure(bind=engine)
    Base.metadata.create_all(engine)
    _migrar(engine)
    with SessionLocal() as db:
        _semear(db)
    return engine


# Colunas adicionadas depois da primeira versão: (tabela, coluna, DDL).
# create_all cria tabelas novas, mas não altera tabelas que já existem.
_COLUNAS_NOVAS = [
    ("lancamentos", "confirmado", "BOOLEAN NOT NULL DEFAULT 0"),
    ("contas", "confirmacao", "BOOLEAN NOT NULL DEFAULT 1"),
    ("lancamentos", "verba_valor", "INTEGER"),
    ("parceladas", "verba_id", "INTEGER REFERENCES recorrentes(id) ON DELETE SET NULL"),
    ("parceladas", "verba_valor", "INTEGER"),
    ("parceladas", "verba_expr", "VARCHAR(255) NOT NULL DEFAULT ''"),
]


def _migrar(engine: Engine) -> None:
    with engine.begin() as conn:
        for tabela, coluna, ddl in _COLUNAS_NOVAS:
            existentes = {linha[1] for linha in conn.exec_driver_sql(f"PRAGMA table_info({tabela})")}
            if coluna not in existentes:
                conn.exec_driver_sql(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {ddl}")


def get_db():
    with SessionLocal() as db:
        yield db


def _semear(db: Session) -> None:
    if not db.scalar(select(Usuario).limit(1)):
        db.add(Usuario(login="admin", senha_hash=hash_senha("admin"), senha_padrao=True))
    if not db.scalar(select(Conta).limit(1)):
        nuconta = Conta(nome="Nuconta", tipo="corrente", ordem=1)
        db.add(nuconta)
        db.flush()
        db.add(Conta(nome="Nubank", tipo="cartao", paga_por_id=nuconta.id, ordem=2))
    db.commit()


def hash_senha(senha: str) -> str:
    sal = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal.encode(), 600_000).hex()
    return f"pbkdf2_sha256$600000${sal}${h}"


def conferir_senha(senha: str, armazenado: str) -> bool:
    try:
        _, iteracoes, sal, h = armazenado.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal.encode(), int(iteracoes)).hex()
    return hmac.compare_digest(calc, h)


def chave_sessao() -> str:
    """Chave persistente para assinar o cookie de sessão."""
    arq = config.DATA_DIR / "secret.key"
    try:
        return arq.read_text().strip()
    except FileNotFoundError:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        chave = secrets.token_urlsafe(48)
        arq.write_text(chave)
        arq.chmod(0o600)
        return chave
