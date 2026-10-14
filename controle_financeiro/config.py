import os
from pathlib import Path

# Pasta dos dados (banco SQLite + chave de sessão). Fica fora do projeto para
# não se perder; no Home Assistant aponte CF_DATA_DIR para /data.
DATA_DIR = Path(os.environ.get("CF_DATA_DIR", Path.home() / ".controle-financeiro")).expanduser()
DB_URL = os.environ.get("CF_DB_URL", f"sqlite:///{DATA_DIR / 'financas.db'}")

HOST = os.environ.get("CF_HOST", "127.0.0.1")
PORT = int(os.environ.get("CF_PORT", "8000"))

# Como na planilha: o saldo do mês anterior só é levado adiante quando negativo.
CARREGAR_APENAS_SALDO_NEGATIVO = os.environ.get("CF_CARREGAR_SALDO_POSITIVO", "0") != "1"
