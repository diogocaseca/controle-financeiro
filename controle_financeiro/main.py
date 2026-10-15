import hashlib
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import config, db as banco, servicos
from .dinheiro import (
    MESES, ExpressaoInvalida, ano_mes, avaliar, competencia, eh_expressao, formatar, nome_competencia,
    para_entrada,
)
from .models import (
    Conta, Controle, FaturaConfirmada, Lancamento, Movimento, Parcelada, Recorrente, Usuario,
)

AQUI = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    if banco.engine is None:
        banco.iniciar()
    yield


class IngressMiddleware:
    """Respeita o prefixo do Ingress do Home Assistant (cabeçalho X-Ingress-Path)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for k, v in scope.get("headers", []):
                if k == b"x-ingress-path":
                    # O Starlette espera que o path venha com o root_path na frente
                    # (e o remove ao rotear); sem isso os arquivos estáticos dão 404.
                    prefixo = v.decode().rstrip("/")
                    path = scope["path"]
                    if not path.startswith(prefixo + "/"):
                        path = prefixo + path
                    scope = dict(scope, root_path=prefixo, path=path, raw_path=path.encode())
                    break
        await self.app(scope, receive, send)


app = FastAPI(title="Controle Financeiro", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=banco.chave_sessao(), same_site="lax", max_age=60 * 60 * 24 * 30)
app.add_middleware(IngressMiddleware)
app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")

templates = Jinja2Templates(directory=AQUI / "templates")
templates.env.filters["brl"] = formatar
templates.env.filters["entrada"] = para_entrada
templates.env.filters["eh_expr"] = eh_expressao
templates.env.globals.update(nome_comp=nome_competencia, MESES=MESES, ano_mes=ano_mes)
# Muda o endereço do CSS sempre que o arquivo muda, para o navegador não usar a versão em cache.
templates.env.globals["css_v"] = hashlib.sha256(
    b"".join((AQUI / "static" / nome).read_bytes() for nome in ("style.css", "app.js"))
).hexdigest()[:10]


def base(request: Request) -> str:
    return request.scope.get("root_path", "")


def ir(request: Request, caminho: str, msg: str | None = None, erro: bool = False) -> RedirectResponse:
    url = base(request) + caminho
    if msg:
        url += ("&" if "?" in url else "?") + ("erro=" if erro else "msg=") + quote(msg)
    return RedirectResponse(url, status_code=303)


def render(request: Request, nome: str, **ctx):
    ctx.setdefault("comp_hoje", comp_hoje())
    return templates.TemplateResponse(
        request, nome, {"base": base(request), "sem_login": dispensa_login(request), **ctx}
    )


def dispensa_login(request: Request) -> bool:
    """Requisição autenticada pelo Home Assistant (Ingress) com login do app desligado."""
    return (
        not config.LOGIN_VIA_INGRESS
        and "x-ingress-path" in request.headers
        and request.client is not None
        and request.client.host == config.INGRESS_PROXY_IP
    )


def comp_hoje() -> int:
    hoje = date.today()
    return competencia(hoje.year, hoje.month)


def comp_de(texto: str) -> int:
    """Converte 'AAAA-MM' (input type=month) em competência."""
    ano, mes = texto.split("-")[:2]
    return competencia(int(ano), int(mes))


def comp_texto(comp: int) -> str:
    ano, mes = ano_mes(comp)
    return f"{ano:04d}-{mes:02d}"


templates.env.filters["mes_input"] = comp_texto


class NaoLogado(Exception):
    pass


@app.exception_handler(NaoLogado)
async def _nao_logado(request: Request, _exc):
    return ir(request, "/login")


@app.exception_handler(ExpressaoInvalida)
async def _expr_invalida(request: Request, exc: ExpressaoInvalida):
    volta = request.headers.get("referer") or base(request) + "/"
    volta = volta.split("?")[0]
    return RedirectResponse(f"{volta}?erro={quote(str(exc))}", status_code=303)


def usuario(request: Request, db: Session = Depends(banco.get_db)) -> Usuario:
    if dispensa_login(request):
        return db.scalar(select(Usuario).order_by(Usuario.id).limit(1))
    uid = request.session.get("uid")
    u = db.get(Usuario, uid) if uid else None
    if not u:
        raise NaoLogado()
    return u


Logado = Depends(usuario)


# ------------------------------------------------------------------ login ---

@app.get("/login")
def login_form(request: Request):
    if dispensa_login(request):
        return ir(request, "/")
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, login: str = Form(...), senha: str = Form(...), db: Session = Depends(banco.get_db)):
    u = db.scalar(select(Usuario).where(Usuario.login == login.strip()))
    if not u or not banco.conferir_senha(senha, u.senha_hash):
        return render(request, "login.html", erro="Usuário ou senha inválidos.", login=login)
    request.session["uid"] = u.id
    return ir(request, "/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return ir(request, "/login")


@app.get("/senha")
def senha_form(request: Request, u: Usuario = Logado):
    return render(request, "senha.html", u=u)


@app.post("/senha")
def senha(
    request: Request, atual: str = Form(...), nova: str = Form(...), confirma: str = Form(...),
    u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    if not banco.conferir_senha(atual, u.senha_hash):
        return ir(request, "/senha", "Senha atual incorreta.", erro=True)
    if len(nova) < 4 or nova != confirma:
        return ir(request, "/senha", "A nova senha precisa ter 4+ caracteres e ser igual à confirmação.", erro=True)
    u = db.get(Usuario, u.id)
    u.senha_hash = banco.hash_senha(nova)
    u.senha_padrao = False
    db.commit()
    return ir(request, "/", "Senha alterada.")


# ------------------------------------------------------------------ meses ---

@app.get("/")
def inicio(request: Request, _u: Usuario = Logado):
    ano, mes = ano_mes(comp_hoje())
    return ir(request, f"/mes/{ano}/{mes}")


@app.get("/mes/{ano}/{mes}")
def ver_mes(request: Request, ano: int, mes: int, u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    if not 1 <= mes <= 12:
        raise HTTPException(404)
    comp = competencia(ano, mes)
    resumo = servicos.resumo_mes(db, comp)
    return render(
        request, "mes.html", u=u, comp=comp, resumo=resumo,
        verbas=servicos.verbas_do_mes(db, comp),
        contas=db.scalars(select(Conta).where(Conta.ativa).order_by(Conta.ordem)).all(),
    )


@app.get("/ano/{ano}")
def ver_ano(request: Request, ano: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    return render(request, "ano.html", ano=ano, meses=servicos.resumo_ano(db, ano))


def _volta_mes(request: Request, comp: int, msg: str | None = None):
    ano, mes = ano_mes(comp)
    return ir(request, f"/mes/{ano}/{mes}", msg)


# ------------------------------------------------------------ lançamentos ---

@app.post("/lancamentos")
def criar_lancamento(
    request: Request, comp: int = Form(...), conta_id: int = Form(...), descricao: str = Form(...),
    valor: str = Form(...), verba_id: str = Form(""),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    db.add(Lancamento(
        conta_id=conta_id, comp=comp, descricao=descricao.strip(), valor=avaliar(valor),
        valor_expr=valor.strip(), verba_id=int(verba_id) if verba_id else None, ordem=50,
    ))
    db.commit()
    return _volta_mes(request, comp)


@app.get("/lancamentos/{lid}")
def editar_lancamento_form(request: Request, lid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    l = db.get(Lancamento, lid) or _404()
    return render(
        request, "lancamento.html", l=l, verbas=servicos.verbas_do_mes(db, l.comp),
        contas=db.scalars(select(Conta).order_by(Conta.ordem)).all(),
    )


@app.post("/lancamentos/{lid}")
def editar_lancamento(
    request: Request, lid: int, descricao: str = Form(...), valor: str = Form(...),
    conta_id: int = Form(...), verba_id: str = Form(""), verba_valor: str = Form(""), mes: str = Form(""),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    l = db.get(Lancamento, lid) or _404()
    volta = l.comp
    l.descricao, l.valor, l.valor_expr = descricao.strip(), avaliar(valor), valor.strip()
    l.conta_id = conta_id
    l.verba_id = int(verba_id) if verba_id else None
    l.verba_valor = avaliar(verba_valor) if l.verba_id and verba_valor.strip() else None
    if mes and not l.recorrente_id and not l.parcelada_id:
        l.comp = comp_de(mes)
        servicos.garantir_mes(db, l.comp)
    db.commit()
    return _volta_mes(request, volta)


@app.post("/lancamentos/{lid}/excluir")
def excluir_lancamento(request: Request, lid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    l = db.get(Lancamento, lid) or _404()
    comp = l.comp
    db.delete(l)
    db.commit()
    return _volta_mes(request, comp)


@app.post("/lancamentos/{lid}/confirmar")
def confirmar_lancamento(request: Request, lid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    l = db.get(Lancamento, lid) or _404()
    l.confirmado = not l.confirmado
    db.commit()
    return _volta_mes(request, l.comp)


@app.post("/faturas/{conta_id}/{comp}/confirmar")
def confirmar_fatura(
    request: Request, conta_id: int, comp: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    db.get(Conta, conta_id) or _404()
    f = db.get(FaturaConfirmada, (conta_id, comp))
    if f:
        db.delete(f)
    else:
        db.add(FaturaConfirmada(conta_id=conta_id, comp=comp))
    db.commit()
    return _volta_mes(request, comp)


def _404():
    raise HTTPException(404)


# ------------------------------------------------------------- recorrentes ---

@app.get("/recorrentes")
def listar_recorrentes(request: Request, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    hoje = comp_hoje()
    todos = db.scalars(select(Recorrente).order_by(Recorrente.conta_id, Recorrente.ordem, Recorrente.id)).all()
    return render(
        request, "recorrentes.html",
        ativos=[r for r in todos if r.fim is None or r.fim >= hoje],
        encerrados=[r for r in todos if r.fim is not None and r.fim < hoje],
        contas=db.scalars(select(Conta).where(Conta.ativa).order_by(Conta.ordem)).all(),
        controles=db.scalars(select(Controle).where(Controle.ativo).order_by(Controle.nome)).all(),
    )


@app.get("/recorrentes/{rid}")
def editar_recorrente_form(request: Request, rid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    r = db.get(Recorrente, rid) or _404()
    return render(
        request, "recorrente.html", r=r,
        contas=db.scalars(select(Conta).order_by(Conta.ordem)).all(),
        controles=db.scalars(select(Controle).order_by(Controle.nome)).all(),
    )


def _preencher_recorrente(r: Recorrente, descricao, conta_id, valor, verba, inicio, fim, controle_id, controle_valor):
    r.descricao = descricao.strip()
    r.conta_id = int(conta_id) if conta_id else None
    r.valor_expr = valor.strip() if r.conta_id else ""
    r.valor = avaliar(valor) if r.conta_id else 0
    r.verba = bool(verba) and r.conta_id is not None
    r.inicio = comp_de(inicio)
    r.fim = comp_de(fim) if fim else None
    r.controle_id = int(controle_id) if controle_id else None
    r.controle_expr = controle_valor.strip() if r.controle_id else ""
    r.controle_valor = avaliar(controle_valor) if r.controle_id else 0
    if not r.conta_id and not r.controle_id:
        raise ExpressaoInvalida("escolha uma conta e/ou um controle")
    if r.fim is not None and r.fim < r.inicio:
        raise ExpressaoInvalida("o fim não pode ser antes do início")


@app.post("/recorrentes")
def criar_recorrente(
    request: Request, descricao: str = Form(...), conta_id: str = Form(""), valor: str = Form(""),
    verba: str = Form(""), inicio: str = Form(...), fim: str = Form(""),
    controle_id: str = Form(""), controle_valor: str = Form(""),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    r = Recorrente()
    _preencher_recorrente(r, descricao, conta_id, valor, verba, inicio, fim, controle_id, controle_valor)
    db.add(r)
    db.flush()
    servicos.sincronizar_recorrente(db, r, r.inicio)
    db.commit()
    return ir(request, "/recorrentes", f"“{r.descricao}” criado.")


@app.post("/recorrentes/{rid}")
def editar_recorrente(
    request: Request, rid: int, descricao: str = Form(...), conta_id: str = Form(""), valor: str = Form(""),
    verba: str = Form(""), inicio: str = Form(...), fim: str = Form(""),
    controle_id: str = Form(""), controle_valor: str = Form(""), aplicar_de: str = Form(...),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    r = db.get(Recorrente, rid) or _404()
    _preencher_recorrente(r, descricao, conta_id, valor, verba, inicio, fim, controle_id, controle_valor)
    servicos.sincronizar_recorrente(db, r, comp_de(aplicar_de))
    db.commit()
    return ir(request, "/recorrentes", f"“{r.descricao}” atualizado.")


@app.post("/recorrentes/{rid}/encerrar")
def encerrar_recorrente(
    request: Request, rid: int, a_partir: str = Form(...),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    r = db.get(Recorrente, rid) or _404()
    nome = r.descricao
    servicos.encerrar_recorrente(db, r, comp_de(a_partir))
    db.commit()
    return ir(request, "/recorrentes", f"“{nome}” encerrado.")


# ------------------------------------------------------------- parceladas ---

@app.get("/parceladas")
def listar_parceladas(request: Request, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    hoje = comp_hoje()
    todas = db.scalars(select(Parcelada).order_by(Parcelada.primeira.desc())).all()
    return render(
        request, "parceladas.html", hoje=hoje,
        andamento=[p for p in todas if p.primeira + p.parcelas - 1 >= hoje],
        quitadas=[p for p in todas if p.primeira + p.parcelas - 1 < hoje],
        cartoes=db.scalars(select(Conta).where(Conta.ativa).order_by(Conta.tipo, Conta.ordem)).all(),
        verbas=_verbas_ativas(db),
    )


def _verbas_ativas(db: Session, incluir: int | None = None) -> list[Recorrente]:
    hoje = comp_hoje()
    q = select(Recorrente).where(Recorrente.verba).order_by(Recorrente.descricao)
    return [r for r in db.scalars(q) if r.fim is None or r.fim >= hoje or r.id == incluir]


@app.get("/parceladas/{pid}")
def editar_parcelada_form(request: Request, pid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    p = db.get(Parcelada, pid) or _404()
    return render(
        request, "parcelada.html", p=p,
        cartoes=db.scalars(select(Conta).order_by(Conta.tipo, Conta.ordem)).all(),
        verbas=_verbas_ativas(db, incluir=p.verba_id),
    )


def _preencher_parcelada(
    p: Parcelada, descricao, conta_id, valor, tipo_valor, parcelas, mes, parcela_no_mes, verba_id="", verba_valor="",
):
    p.verba_id = int(verba_id) if verba_id else None
    p.verba_expr = verba_valor.strip() if p.verba_id else ""
    p.verba_valor = avaliar(verba_valor) if p.verba_id and verba_valor.strip() else None
    p.descricao = descricao.strip()
    p.conta_id = conta_id
    p.parcelas = parcelas
    total = avaliar(valor)
    if total > 0:
        total = -total  # compra é sempre saída
    p.valor_total = total * parcelas if tipo_valor == "parcela" else total
    p.valor_expr = valor.strip()
    if not 1 <= parcela_no_mes <= parcelas or parcelas < 1:
        raise ExpressaoInvalida("parcela inválida")
    p.primeira = comp_de(mes) - (parcela_no_mes - 1)
    p.lancada_desde = parcela_no_mes


@app.post("/parceladas")
def criar_parcelada(
    request: Request, descricao: str = Form(...), conta_id: int = Form(...), valor: str = Form(...),
    tipo_valor: str = Form("total"), parcelas: int = Form(...), mes: str = Form(...),
    parcela_no_mes: int = Form(1), verba_id: str = Form(""), verba_valor: str = Form(""),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    p = Parcelada()
    _preencher_parcelada(
        p, descricao, conta_id, valor, tipo_valor, parcelas, mes, parcela_no_mes, verba_id, verba_valor,
    )
    db.add(p)
    db.flush()
    servicos.gerar_parcelas(db, p)
    db.commit()
    return ir(request, "/parceladas", f"“{p.descricao}” lançada em {p.parcelas - p.lancada_desde + 1} parcela(s).")


@app.post("/parceladas/{pid}")
def editar_parcelada(
    request: Request, pid: int, descricao: str = Form(...), conta_id: int = Form(...), valor: str = Form(...),
    tipo_valor: str = Form("total"), parcelas: int = Form(...), mes: str = Form(...),
    parcela_no_mes: int = Form(1), verba_id: str = Form(""), verba_valor: str = Form(""),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    p = db.get(Parcelada, pid) or _404()
    _preencher_parcelada(
        p, descricao, conta_id, valor, tipo_valor, parcelas, mes, parcela_no_mes, verba_id, verba_valor,
    )
    servicos.gerar_parcelas(db, p)
    db.commit()
    return ir(request, "/parceladas", f"“{p.descricao}” atualizada.")


@app.post("/parceladas/{pid}/excluir")
def excluir_parcelada(request: Request, pid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    p = db.get(Parcelada, pid) or _404()
    db.delete(p)
    db.commit()
    return ir(request, "/parceladas", "Compra excluída.")


# --------------------------------------------------------------- controles ---

@app.get("/controles")
def listar_controles(request: Request, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    controles = db.scalars(select(Controle).order_by(Controle.ativo.desc(), Controle.grupo, Controle.nome)).all()
    return render(request, "controles.html", controles=[(c, sum(m.valor for m in c.movimentos)) for c in controles])


@app.post("/controles")
def criar_controle(
    request: Request, nome: str = Form(...), grupo: str = Form("divida"), descricao: str = Form(""),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    c = Controle(nome=nome.strip(), grupo=grupo, descricao=descricao.strip())
    db.add(c)
    db.commit()
    return ir(request, f"/controles/{c.id}")


@app.get("/controles/{cid}")
def ver_controle(request: Request, cid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    c = db.get(Controle, cid) or _404()
    acumulado, linhas = 0, []
    for m in c.movimentos:
        acumulado += m.valor
        linhas.append((m, acumulado))
    return render(request, "controle.html", c=c, linhas=list(reversed(linhas)), saldo=acumulado)


@app.post("/controles/{cid}")
def editar_controle(
    request: Request, cid: int, nome: str = Form(...), grupo: str = Form("divida"), descricao: str = Form(""),
    ativo: str = Form(""), _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    c = db.get(Controle, cid) or _404()
    c.nome, c.grupo, c.descricao, c.ativo = nome.strip(), grupo, descricao.strip(), bool(ativo)
    db.commit()
    return ir(request, f"/controles/{cid}", "Salvo.")


@app.post("/controles/{cid}/excluir")
def excluir_controle(request: Request, cid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    c = db.get(Controle, cid) or _404()
    db.delete(c)
    db.commit()
    return ir(request, "/controles", "Controle excluído.")


@app.post("/controles/{cid}/movimentos")
def criar_movimento(
    request: Request, cid: int, mes: str = Form(...), descricao: str = Form(...), valor: str = Form(...),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    c = db.get(Controle, cid) or _404()
    db.add(Movimento(controle_id=c.id, comp=comp_de(mes), descricao=descricao.strip(),
                     valor=avaliar(valor), valor_expr=valor.strip()))
    db.commit()
    return ir(request, f"/controles/{cid}")


@app.get("/movimentos/{mid}")
def editar_movimento_form(request: Request, mid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    m = db.get(Movimento, mid) or _404()
    return render(request, "movimento.html", m=m, controles=db.scalars(select(Controle).order_by(Controle.nome)).all())


@app.post("/movimentos/{mid}")
def editar_movimento(
    request: Request, mid: int, descricao: str = Form(...), valor: str = Form(...),
    controle_id: int = Form(...), mes: str = Form(""),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    m = db.get(Movimento, mid) or _404()
    volta = m.controle_id
    m.descricao, m.valor, m.valor_expr = descricao.strip(), avaliar(valor), valor.strip()
    m.controle_id = controle_id
    if mes and not m.recorrente_id:
        m.comp = comp_de(mes)
    db.commit()
    return ir(request, f"/controles/{volta}", "Movimento atualizado.")


@app.post("/movimentos/{mid}/excluir")
def excluir_movimento(request: Request, mid: int, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    m = db.get(Movimento, mid) or _404()
    cid = m.controle_id
    db.delete(m)
    db.commit()
    return ir(request, f"/controles/{cid}")


# ------------------------------------------------------------------ contas ---

@app.get("/contas")
def listar_contas(request: Request, _u: Usuario = Logado, db: Session = Depends(banco.get_db)):
    return render(request, "contas.html", contas=db.scalars(select(Conta).order_by(Conta.ordem, Conta.id)).all())


@app.post("/contas")
def salvar_conta(
    request: Request, id: str = Form(""), nome: str = Form(...), tipo: str = Form("corrente"),
    paga_por_id: str = Form(""), ordem: int = Form(0), ativa: str = Form(""), confirmacao: str = Form(""),
    _u: Usuario = Logado, db: Session = Depends(banco.get_db),
):
    c = db.get(Conta, int(id)) if id else Conta()
    c.nome, c.tipo, c.ordem = nome.strip(), tipo, ordem
    c.confirmacao = bool(confirmacao)
    c.paga_por_id = int(paga_por_id) if paga_por_id and tipo == "cartao" else None
    c.ativa = bool(ativa) if id else True
    db.add(c)
    db.commit()
    return ir(request, "/contas", "Conta salva.")


def run():
    import uvicorn

    uvicorn.run("controle_financeiro.main:app", host=config.HOST, port=config.PORT, proxy_headers=True)
