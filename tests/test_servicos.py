import pytest
from sqlalchemy import select

from controle_financeiro import db as banco, servicos
from controle_financeiro.dinheiro import avaliar, competencia, formatar
from controle_financeiro.models import Conta, Controle, Lancamento, Parcelada, Recorrente

JAN = competencia(2027, 1)


@pytest.fixture
def db():
    banco.iniciar("sqlite://")
    with banco.SessionLocal() as s:
        yield s


def contas(db):
    nuconta = db.scalar(select(Conta).where(Conta.tipo == "corrente"))
    nubank = db.scalar(select(Conta).where(Conta.tipo == "cartao"))
    return nuconta, nubank


def fixo(db, conta, descricao, expr, inicio=JAN, **kw):
    r = Recorrente(descricao=descricao, conta_id=conta.id if conta else None, valor=avaliar(expr) if expr else 0,
                   valor_expr=expr, inicio=inicio, **kw)
    db.add(r)
    db.flush()
    servicos.sincronizar_recorrente(db, r, r.inicio)
    db.commit()
    return r


@pytest.mark.parametrize("expr,centavos", [
    ("10000", 1_000_000),
    ("-156.98/2", -7849),
    ("-156,98/2", -7849),
    ("=-175-53.9-(-19.9-10)", -19900),
    ("-1.951,30/10", -19513),
    ("R$ 1.234,56", 123456),
])
def test_avaliar(expr, centavos):
    assert avaliar(expr) == centavos


def test_formatar():
    assert formatar(-123456) == "-1.234,56"
    assert formatar(5, sinal=True) == "+0,05"


def test_fixos_saldo_e_fatura(db):
    nuconta, nubank = contas(db)
    fixo(db, nuconta, "Salário", "10000")
    fixo(db, nuconta, "TIM", "-156.98/2")
    fixo(db, nubank, "Assinaturas", "-175-53.9-(-19.9-10)")

    r = servicos.resumo_mes(db, JAN)
    rc = r.contas[0]
    assert rc.cartoes[0].total == -19900
    assert rc.saldo == 1_000_000 - 7849 - 19900


def test_saldo_anterior_so_negativo(db):
    nuconta, _ = contas(db)
    fixo(db, nuconta, "Aluguel", "-100")
    db.add(Lancamento(conta_id=nuconta.id, comp=JAN, descricao="Salário", valor=avaliar("50")))
    servicos.garantir_mes(db, JAN)
    db.commit()

    # Jan: +50 - 100 = -50 → carregado para fevereiro
    fev = servicos.resumo_mes(db, JAN + 1).contas[0]
    assert fev.saldo_anterior == -5000
    assert fev.saldo == -15000

    # Positivo não é carregado
    db.add(Lancamento(conta_id=nuconta.id, comp=JAN + 1, descricao="Bônus", valor=100_000))
    db.commit()
    mar = servicos.resumo_mes(db, JAN + 2).contas[0]
    assert mar.saldo_anterior == 0


def test_mes_pulado_e_preenchido(db):
    nuconta, _ = contas(db)
    fixo(db, nuconta, "Conta", "-10")
    servicos.garantir_mes(db, JAN)
    servicos.resumo_mes(db, JAN + 3)
    # fevereiro e março foram gerados para manter o saldo encadeado
    assert servicos.resumo_mes(db, JAN + 3).contas[0].saldo_anterior == -3000


def test_parcelada_em_andamento(db):
    _, nubank = contas(db)
    # Lava louça: em jan/2027 cai a parcela 3 de 10
    p = Parcelada(descricao="Lava louça", conta_id=nubank.id, valor_total=avaliar("-1951,30"),
                  parcelas=10, primeira=JAN - 2, lancada_desde=3)
    db.add(p)
    db.flush()
    servicos.gerar_parcelas(db, p)
    db.commit()

    parcelas = db.scalars(select(Lancamento).where(Lancamento.parcelada_id == p.id).order_by(Lancamento.comp)).all()
    assert [l.parcela for l in parcelas] == list(range(3, 11))
    assert parcelas[0].comp == JAN
    assert parcelas[-1].comp == JAN + 7
    assert sum(p.valor_parcela(n) for n in range(1, 11)) == -195130

    cartao = servicos.resumo_mes(db, JAN).contas[0].cartoes[0]
    assert cartao.total == -19513


def test_verba_desconta_gasto_no_cartao(db):
    nuconta, nubank = contas(db)
    limpeza = fixo(db, nuconta, "Limpeza", "-600", verba=True)
    servicos.garantir_mes(db, JAN)
    db.add(Lancamento(conta_id=nubank.id, comp=JAN, descricao="Hortifruti", valor=-20000, verba_id=limpeza.id))
    db.commit()

    rc = servicos.resumo_mes(db, JAN).contas[0]
    linha = next(l for l in rc.linhas if l.lancamento.eh_verba)
    assert linha.efetivo == -40000
    # Total sai igual a 600 (400 em dinheiro + 200 no cartão)
    assert rc.saldo == -60000


def test_verba_zero_vira_reembolso(db):
    nuconta, nubank = contas(db)
    dal = fixo(db, nuconta, "DAL Solutions", "0", verba=True)
    servicos.garantir_mes(db, JAN)
    db.add(Lancamento(conta_id=nubank.id, comp=JAN, descricao="Assinatura", valor=-91737, verba_id=dal.id))
    db.commit()

    rc = servicos.resumo_mes(db, JAN).contas[0]
    linha = next(l for l in rc.linhas if l.lancamento.eh_verba)
    assert linha.efetivo == 91737  # a empresa devolve
    assert rc.cartoes[0].total == -91737
    assert rc.saldo == 0  # fatura e reembolso se anulam


def test_verba_estourada_vira_entrada(db):
    nuconta, nubank = contas(db)
    limpeza = fixo(db, nuconta, "Limpeza", "-600", verba=True)
    servicos.garantir_mes(db, JAN)
    db.add(Lancamento(conta_id=nubank.id, comp=JAN, descricao="Mercado", valor=-70000, verba_id=limpeza.id))
    db.commit()

    linha = next(l for l in servicos.resumo_mes(db, JAN).contas[0].linhas if l.lancamento.eh_verba)
    assert linha.efetivo == 10000


def test_controle_acumula(db):
    nuconta, _ = contas(db)
    fab = Controle(nome="Fabiane")
    db.add(fab)
    db.flush()
    fixo(db, nuconta, "Light", "-200", controle_id=fab.id, controle_valor=20000, controle_expr="200")
    fixo(db, None, "Condomínio (parte dela)", "", controle_id=fab.id, controle_valor=-99925, controle_expr="-999,25")
    servicos.garantir_mes(db, JAN)
    servicos.garantir_mes(db, JAN + 1)

    ctl = servicos.resumo_mes(db, JAN + 1).controles[0]
    assert ctl.saldo == 2 * (20000 - 99925)
    assert len(ctl.movimentos_mes) == 2


def test_editar_fixo_a_partir_de(db):
    nuconta, _ = contas(db)
    r = fixo(db, nuconta, "Light", "-200")
    servicos.garantir_mes(db, JAN)
    servicos.garantir_mes(db, JAN + 2)
    r.valor = -25000
    servicos.sincronizar_recorrente(db, r, JAN + 1)
    db.commit()

    valores = {l.comp: l.valor for l in db.scalars(select(Lancamento).where(Lancamento.recorrente_id == r.id))}
    assert valores == {JAN: -20000, JAN + 1: -25000, JAN + 2: -25000}

    servicos.encerrar_recorrente(db, r, JAN + 2)
    db.commit()
    assert set(l.comp for l in db.scalars(select(Lancamento).where(Lancamento.recorrente_id == r.id))) == {JAN, JAN + 1}
    assert servicos.resumo_mes(db, JAN + 3).contas[0].linhas == []


def test_parcelada_desconta_da_verba(db):
    nuconta, nubank = contas(db)
    limpeza = fixo(db, nuconta, "Limpeza", "-600", verba=True)
    servicos.garantir_mes(db, JAN)
    p = Parcelada(descricao="Lava louça", conta_id=nubank.id, valor_total=-195130, parcelas=10,
                  primeira=JAN, lancada_desde=1, verba_id=limpeza.id, verba_valor=19513)
    db.add(p)
    db.flush()
    servicos.gerar_parcelas(db, p)
    db.commit()

    rc = servicos.resumo_mes(db, JAN).contas[0]
    linha = next(l for l in rc.linhas if l.lancamento.eh_verba)
    assert linha.efetivo == -60000 + 19513
    assert rc.cartoes[0].total == -19513

    # Valor aplicado diferente da parcela (ex.: só metade entra na verba)
    p.verba_valor = 10000
    servicos.gerar_parcelas(db, p)
    db.commit()
    linha = next(l for l in servicos.resumo_mes(db, JAN).contas[0].linhas if l.lancamento.eh_verba)
    assert linha.efetivo == -50000


def test_ano_preve_meses_nao_abertos_sem_gravar(db):
    from controle_financeiro.models import MesGerado

    nuconta, nubank = contas(db)
    fixo(db, nuconta, "Salário", "100")
    fixo(db, nuconta, "Aluguel", "-150")  # saldo negativo é carregado
    limpeza = fixo(db, nuconta, "Limpeza", "-60", verba=True)
    servicos.garantir_mes(db, JAN)
    p = Parcelada(descricao="TV", conta_id=nubank.id, valor_total=-3000, parcelas=3, primeira=JAN + 1,
                  lancada_desde=1, verba_id=limpeza.id)
    db.add(p)
    db.flush()
    servicos.gerar_parcelas(db, p)
    db.add(Lancamento(conta_id=nuconta.id, comp=JAN + 2, descricao="Bônus", valor=5000))
    db.commit()

    antes = (db.query(Lancamento).count(), db.query(MesGerado).count())
    ano = servicos.resumo_ano(db, 2027)
    assert (db.query(Lancamento).count(), db.query(MesGerado).count()) == antes  # nada gravado

    previstos = {c: (rs[0].saldo, prev) for c, rs, prev in ano if rs}
    assert previstos[JAN][1] is False and previstos[JAN + 1][1] is True
    assert len(previstos) == 12

    # Abrindo os meses de verdade, os saldos batem com a previsão
    for c in range(JAN, JAN + 12):
        assert servicos.resumo_mes(db, c).contas[0].saldo == previstos[c][0], c
    assert all(not prev for _, _, prev in servicos.resumo_ano(db, 2027))


def test_ano_antes_do_inicio(db):
    nuconta, _ = contas(db)
    fixo(db, nuconta, "Salário", "100", inicio=JAN + 9)
    servicos.garantir_mes(db, JAN + 9)
    ano = servicos.resumo_ano(db, 2027)
    assert [rs is None for _, rs, _ in ano] == [True] * 9 + [False] * 3
    assert servicos.resumo_ano(db, 2026) == [(c, None, False) for c in range(2026 * 12, 2026 * 12 + 12)]
