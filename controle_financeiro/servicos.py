"""Regras de negócio: geração dos meses, parcelas, verbas e saldos."""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import config
from .dinheiro import ano_mes, nome_competencia
from .models import (
    Conta, Controle, FaturaConfirmada, Lancamento, MesGerado, Movimento, Parcelada, Recorrente, TravaFatura,
)


# ------------------------------------------------------------ trava fatura ---

class MesTravado(ValueError):
    pass


def hoje() -> date:
    return date.today()


@dataclass
class EstadoTrava:
    travada: bool
    motivo: str = ""  # "fechamento" | "manual" | "destravada" | ""
    fechamento: date | None = None
    ate: date | None = None  # destravada até este dia


def data_fechamento(conta: Conta, comp: int) -> date | None:
    """Dia em que fecha a fatura do mês (no próprio mês; dia 31 vira o último dia)."""
    if not conta.eh_cartao or not conta.dia_fechamento:
        return None
    ano, mes = ano_mes(comp)
    return date(ano, mes, min(conta.dia_fechamento, calendar.monthrange(ano, mes)[1]))


def estado_trava(db: Session, conta: Conta, comp: int) -> EstadoTrava:
    if not conta.eh_cartao:
        return EstadoTrava(False)
    dia = hoje()
    fech = data_fechamento(conta, comp)
    o = db.get(TravaFatura, (conta.id, comp))
    if o and o.travada:
        return EstadoTrava(True, "manual", fech)
    if o and o.ate and o.ate >= dia:
        return EstadoTrava(False, "destravada", fech, o.ate)
    auto = fech is not None and dia >= fech
    return EstadoTrava(auto, "fechamento" if auto else "", fech)


def esta_travada(db: Session, conta_id: int, comp: int) -> bool:
    conta = db.get(Conta, conta_id)
    return bool(conta) and estado_trava(db, conta, comp).travada


def exigir_destravada(db: Session, conta_id: int, comp: int) -> None:
    if esta_travada(db, conta_id, comp):
        conta = db.get(Conta, conta_id)
        raise MesTravado(f"a fatura de {nome_competencia(comp)} do {conta.nome} está travada. "
                         f"Destrave no mês para alterar.")


def alternar_trava(db: Session, conta: Conta, comp: int) -> EstadoTrava:
    """Travada → destrava até o fim de hoje. Aberta → trava (manual, ou volta à automática)."""
    atual = estado_trava(db, conta, comp)
    o = db.get(TravaFatura, (conta.id, comp))
    if atual.travada:
        if o:
            o.travada, o.ate = False, hoje()
        else:
            db.add(TravaFatura(conta_id=conta.id, comp=comp, travada=False, ate=hoje()))
    else:
        fech = data_fechamento(conta, comp)
        if fech is not None and hoje() >= fech:
            if o:
                db.delete(o)  # volta à trava automática
        elif o:
            o.travada, o.ate = True, None
        else:
            db.add(TravaFatura(conta_id=conta.id, comp=comp, travada=True))
    db.flush()
    return estado_trava(db, conta, comp)


# ---------------------------------------------------------------- geração ---

def garantir_mes(db: Session, comp: int) -> None:
    """Lança os recorrentes do mês na primeira vez que ele é aberto.

    Se já houver meses gerados antes, preenche também os meses intermediários,
    para que o saldo anterior seja sempre levado adiante sem buracos.
    """
    if db.get(MesGerado, comp):
        return
    ultimo = db.scalar(select(func.max(MesGerado.comp)).where(MesGerado.comp < comp))
    inicio = ultimo + 1 if ultimo is not None else comp
    recorrentes = db.scalars(select(Recorrente)).all()
    for c in range(inicio, comp + 1):
        if db.get(MesGerado, c):
            continue
        for r in recorrentes:
            if r.ativa_em(c):
                _aplicar_recorrente(db, r, c)
        db.add(MesGerado(comp=c))
    db.commit()


def _aplicar_recorrente(db: Session, r: Recorrente, comp: int, confirmado: bool = False) -> None:
    if r.conta_id:
        db.add(Lancamento(
            conta_id=r.conta_id, comp=comp, descricao=r.descricao, valor=r.valor,
            valor_expr=r.valor_expr, recorrente_id=r.id, eh_verba=r.verba, ordem=r.ordem,
            confirmado=confirmado,
        ))
    if r.controle_id and r.controle_valor:
        db.add(Movimento(
            controle_id=r.controle_id, comp=comp, descricao=r.descricao,
            valor=r.controle_valor, valor_expr=r.controle_expr, recorrente_id=r.id,
        ))


def sincronizar_recorrente(db: Session, r: Recorrente, a_partir: int) -> set[int]:
    """Refaz os lançamentos do recorrente nos meses já gerados a partir de `a_partir`.

    Alterações manuais feitas nesses meses (para esse recorrente) são substituídas;
    meses anteriores ficam como estão.
    Meses com a fatura do cartão travada não são alterados. Devolve esses meses.
    """
    db.flush()
    existentes = db.scalars(select(Lancamento).where(
        Lancamento.recorrente_id == r.id, Lancamento.comp >= a_partir)).all()
    confirmados = {l.comp for l in existentes if l.confirmado}
    meses = db.scalars(select(MesGerado.comp).where(MesGerado.comp >= a_partir)).all()
    protegidos = {l.comp for l in existentes if esta_travada(db, l.conta_id, l.comp)}
    if r.conta_id:
        protegidos |= {c for c in meses if esta_travada(db, r.conta_id, c)}
    db.execute(delete(Lancamento).where(Lancamento.recorrente_id == r.id, Lancamento.comp >= a_partir,
                                        Lancamento.comp.not_in(protegidos)))
    db.execute(delete(Movimento).where(Movimento.recorrente_id == r.id, Movimento.comp >= a_partir,
                                       Movimento.comp.not_in(protegidos)))
    for c in meses:
        if r.ativa_em(c) and c not in protegidos:
            _aplicar_recorrente(db, r, c, confirmado=c in confirmados)
    return protegidos


def encerrar_recorrente(db: Session, r: Recorrente, a_partir: int) -> None:
    """Para de lançar o recorrente a partir de `a_partir` (o histórico anterior fica)."""
    if a_partir <= r.inicio:
        excluir_recorrente(db, r)
        return
    r.fim = a_partir - 1
    sincronizar_recorrente(db, r, a_partir)


def excluir_recorrente(db: Session, r: Recorrente) -> None:
    """Apaga o fixo e tudo o que ele lançou em todos os meses.

    Gastos no cartão e parcelados que usavam o fixo como verba ficam sem verba.
    """
    _recusar_se_travado(db, select(Lancamento).where(Lancamento.recorrente_id == r.id), "o fixo")
    db.execute(delete(Lancamento).where(Lancamento.recorrente_id == r.id))
    db.execute(delete(Movimento).where(Movimento.recorrente_id == r.id))
    db.execute(update(Lancamento).where(Lancamento.verba_id == r.id).values(verba_id=None, verba_valor=None))
    db.execute(update(Parcelada).where(Parcelada.verba_id == r.id).values(verba_id=None, verba_valor=None, verba_expr=""))
    db.delete(r)


def gerar_parcelas(db: Session, p: Parcelada) -> set[int]:
    """(Re)gera as parcelas. Parcelas em meses travados ficam como estão; uma parcela
    nova que cairia num mês travado é recusada. Devolve os números preservados."""
    db.flush()
    existentes = db.scalars(select(Lancamento).where(Lancamento.parcelada_id == p.id)).all()
    confirmadas = {l.parcela for l in existentes if l.confirmado}
    protegidas = {l.parcela for l in existentes if esta_travada(db, l.conta_id, l.comp)}
    for l in existentes:
        if l.parcela not in protegidas:
            db.delete(l)
    db.flush()
    for n in range(max(1, p.lancada_desde), p.parcelas + 1):
        if n in protegidas:
            continue
        exigir_destravada(db, p.conta_id, p.primeira + n - 1)
        db.add(Lancamento(
            conta_id=p.conta_id, comp=p.primeira + n - 1, descricao=p.descricao,
            valor=p.valor_parcela(n), parcelada_id=p.id, parcela=n, ordem=100,
            confirmado=n in confirmadas, verba_id=p.verba_id, verba_valor=p.verba_valor,
        ))
    return protegidas


def excluir_parcelada(db: Session, p: Parcelada) -> None:
    _recusar_se_travado(db, select(Lancamento).where(Lancamento.parcelada_id == p.id), "a compra")
    db.delete(p)


def _recusar_se_travado(db: Session, consulta, oque: str) -> None:
    travados = sorted({l.comp for l in db.scalars(consulta) if esta_travada(db, l.conta_id, l.comp)})
    if travados:
        meses = ", ".join(nome_competencia(c) for c in travados)
        raise MesTravado(f"{oque} tem lançamentos em fatura travada ({meses}). "
                         f"Destrave esses meses para excluir, ou use “Encerrar”.")


# ----------------------------------------------------------------- resumo ---

@dataclass
class Linha:
    lancamento: Lancamento
    efetivo: int  # valor que entra no saldo (verba já descontada dos gastos no cartão)
    gasto_cartao: int = 0


@dataclass
class Cartao:
    conta: Conta
    linhas: list[Linha]
    total: int
    confirmada: bool = False
    trava: EstadoTrava | None = None


@dataclass
class ResumoConta:
    conta: Conta
    saldo_anterior: int
    linhas: list[Linha]
    cartoes: list[Cartao]
    saldo: int
    entradas: int = 0
    saidas: int = 0
    pendentes: int = 0  # quantos itens ainda não foram confirmados
    valor_pendente: int = 0


@dataclass
class ResumoControle:
    controle: Controle
    movimentos_mes: list[Movimento]
    saldo: int


@dataclass
class ResumoMes:
    comp: int
    contas: list[ResumoConta] = field(default_factory=list)
    controles: list[ResumoControle] = field(default_factory=list)
    cartoes_avulsos: list[Cartao] = field(default_factory=list)


def _contas(db: Session) -> list[Conta]:
    return list(db.scalars(select(Conta).where(Conta.ativa).order_by(Conta.ordem, Conta.id)))


def _lancamentos(db: Session, comp: int) -> dict[int, list[Lancamento]]:
    por_conta: dict[int, list[Lancamento]] = defaultdict(list)
    # Fixos sempre no topo (na ordem definida no fixo); avulsos e parcelas abaixo.
    q = select(Lancamento).where(Lancamento.comp == comp).order_by(
        Lancamento.recorrente_id.is_(None), Lancamento.ordem, Lancamento.id)
    for l in db.scalars(q):
        por_conta[l.conta_id].append(l)
    return por_conta


def _montar(
    db: Session, comp: int, contas: list[Conta], saldo_anterior: dict[int, int],
    previsao: list[Recorrente] | None = None,
):
    """Monta o resumo do mês. Com `previsao`, soma os fixos ativos como se o mês
    já tivesse sido aberto, sem gravar nada (usado no resumo anual)."""
    lancs = _lancamentos(db, comp)
    for r in previsao or []:
        if r.conta_id and r.ativa_em(comp):
            lancs[r.conta_id].append(Lancamento(
                conta_id=r.conta_id, comp=comp, descricao=r.descricao, valor=r.valor,
                recorrente_id=r.id, eh_verba=r.verba, confirmado=False,
            ))
    gasto_verba: dict[int, int] = defaultdict(int)
    for c in contas:
        if c.eh_cartao:
            for l in lancs[c.id]:
                if l.verba_id:
                    gasto_verba[l.verba_id] -= l.desconto_verba

    faturas_ok = set(db.scalars(select(FaturaConfirmada.conta_id).where(FaturaConfirmada.comp == comp)))
    cartoes: dict[int, Cartao] = {}
    for c in contas:
        if c.eh_cartao:
            linhas = [Linha(l, l.valor) for l in lancs[c.id]]
            cartoes[c.id] = Cartao(c, linhas, sum(l.valor for l in lancs[c.id]), c.id in faturas_ok)

    resumos = []
    for c in contas:
        if c.eh_cartao:
            continue
        linhas = []
        for l in lancs[c.id]:
            if l.eh_verba and l.recorrente_id:
                gasto = gasto_verba.get(l.recorrente_id, 0)
                # Sem trava em zero: gasto no cartão acima da verba vira entrada
                # (ex.: reembolso da empresa, ou a Limpeza quando ela gasta mais que o combinado).
                linhas.append(Linha(l, l.valor - gasto, gasto))
            else:
                linhas.append(Linha(l, l.valor))
        meus_cartoes = [k for k in cartoes.values() if k.conta.paga_por_id == c.id]
        anterior = saldo_anterior.get(c.id, 0)
        valores = [ln.efetivo for ln in linhas] + [k.total for k in meus_cartoes]
        abertos = []
        if c.confirmacao:
            abertos = [ln.efetivo for ln in linhas if not ln.lancamento.confirmado and ln.efetivo] + [
                k.total for k in meus_cartoes if not k.confirmada and k.total]
        resumos.append(ResumoConta(
            conta=c, saldo_anterior=anterior, linhas=linhas, cartoes=meus_cartoes,
            saldo=anterior + sum(valores),
            entradas=sum(v for v in valores if v > 0), saidas=sum(v for v in valores if v < 0),
            pendentes=len(abertos), valor_pendente=sum(abertos),
        ))
    pagos = {k.conta.id for r in resumos for k in r.cartoes}
    avulsos = [k for k in cartoes.values() if k.conta.id not in pagos]
    return resumos, avulsos


def _carregar(saldo: int) -> int:
    if config.CARREGAR_APENAS_SALDO_NEGATIVO:
        return min(0, saldo)
    return saldo


def resumo_mes(db: Session, comp: int) -> ResumoMes:
    garantir_mes(db, comp)
    contas = _contas(db)

    # Encadeia os saldos desde o primeiro mês gerado da sequência contínua.
    gerados = set(db.scalars(select(MesGerado.comp).where(MesGerado.comp < comp)))
    inicio = comp
    while inicio - 1 in gerados:
        inicio -= 1
    anterior: dict[int, int] = {}
    for c in range(inicio, comp):
        resumos, _ = _montar(db, c, contas, anterior)
        anterior = {r.conta.id: _carregar(r.saldo) for r in resumos}

    resumo = ResumoMes(comp=comp)
    resumo.contas, resumo.cartoes_avulsos = _montar(db, comp, contas, anterior)
    for k in [k for rc in resumo.contas for k in rc.cartoes] + resumo.cartoes_avulsos:
        k.trava = estado_trava(db, k.conta, comp)
    resumo.controles = resumo_controles(db, comp)
    return resumo


def resumo_controles(db: Session, comp: int) -> list[ResumoControle]:
    saida = []
    for ctl in db.scalars(select(Controle).where(Controle.ativo).order_by(Controle.grupo, Controle.nome)):
        movs = [m for m in ctl.movimentos if m.comp == comp]
        saldo = sum(m.valor for m in ctl.movimentos if m.comp <= comp)
        saida.append(ResumoControle(ctl, movs, saldo))
    return saida


def resumo_ano(db: Session, ano: int) -> list[tuple[int, list[ResumoConta] | None, bool]]:
    """Resumo de cada mês do ano: (competência, resumos, previsto).

    Meses ainda não abertos a partir do primeiro mês usado no sistema são
    previstos (fixos ativos + parcelas e lançamentos já existentes), sem gravar
    nada. Meses anteriores ao início do uso ficam como None.
    """
    gerados = set(db.scalars(select(MesGerado.comp)))
    primeiro, ultimo = ano * 12, ano * 12 + 11
    if not gerados or min(gerados) > ultimo:
        return [(c, None, False) for c in range(primeiro, ultimo + 1)]

    contas = _contas(db)
    recorrentes = list(db.scalars(select(Recorrente)))
    inicio = min(gerados)
    meses = [(c, None, False) for c in range(primeiro, min(inicio, ultimo + 1))]
    anterior: dict[int, int] = {}
    for c in range(inicio, ultimo + 1):
        previsto = c not in gerados
        resumos, _ = _montar(db, c, contas, anterior, recorrentes if previsto else None)
        anterior = {r.conta.id: _carregar(r.saldo) for r in resumos}
        if c >= primeiro:
            meses.append((c, resumos, previsto))
    return meses


def verbas_do_mes(db: Session, comp: int) -> list[Recorrente]:
    q = select(Recorrente).where(Recorrente.verba).order_by(Recorrente.descricao)
    return [r for r in db.scalars(q) if r.ativa_em(comp)]
