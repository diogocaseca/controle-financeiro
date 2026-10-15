import os
import tempfile

# Mantém a chave de sessão dos testes fora de ~/.controle-financeiro.
os.environ.setdefault("CF_DATA_DIR", tempfile.mkdtemp(prefix="cf-testes-"))
os.environ.setdefault("CF_BACKUP_AUTOMATICO", "0")
