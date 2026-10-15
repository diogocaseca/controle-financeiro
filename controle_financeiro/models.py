from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Todos os valores são em centavos; negativo = saída, positivo = entrada
# (mesma convenção da planilha). "comp" é a competência: ano * 12 + (mes - 1).


class Base(DeclarativeBase):
    pass


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(50), unique=True)
    senha_hash: Mapped[str] = mapped_column(String(255))
    senha_padrao: Mapped[bool] = mapped_column(Boolean, default=True)


class Conta(Base):
    """Conta corrente (ex.: Nuconta) ou cartão de crédito (ex.: Nubank)."""

    __tablename__ = "contas"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))
    tipo: Mapped[str] = mapped_column(String(10))  # "corrente" | "cartao"
    # Cartão: conta corrente onde a fatura é debitada.
    paga_por_id: Mapped[int | None] = mapped_column(ForeignKey("contas.id", ondelete="SET NULL"))
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    # Mostra a marcação ✓ de confirmado nos lançamentos desta conta.
    confirmacao: Mapped[bool] = mapped_column(Boolean, default=True)

    paga_por: Mapped["Conta | None"] = relationship(remote_side="Conta.id")

    @property
    def eh_cartao(self) -> bool:
        return self.tipo == "cartao"


class Controle(Base):
    """Saldo acompanhado ao longo do tempo: acerto com alguém, empréstimo, poupança."""

    __tablename__ = "controles"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))
    grupo: Mapped[str] = mapped_column(String(20), default="divida")  # "divida" | "poupanca"
    descricao: Mapped[str] = mapped_column(String(255), default="")
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)

    movimentos: Mapped[list["Movimento"]] = relationship(
        back_populates="controle", cascade="all, delete-orphan", order_by="Movimento.comp"
    )


class Recorrente(Base):
    """Lançamento que se repete todo mês (conta fixa, salário, verba de orçamento).

    Pode também gerar um movimento num controle (ex.: parte da Fabiane numa conta
    dividida). Sem conta, gera apenas o movimento no controle.
    """

    __tablename__ = "recorrentes"

    id: Mapped[int] = mapped_column(primary_key=True)
    descricao: Mapped[str] = mapped_column(String(120))
    conta_id: Mapped[int | None] = mapped_column(ForeignKey("contas.id", ondelete="CASCADE"))
    valor: Mapped[int] = mapped_column(Integer, default=0)
    valor_expr: Mapped[str] = mapped_column(String(255), default="")
    # Verba: orçamento; gastos no cartão marcados com ela são descontados.
    verba: Mapped[bool] = mapped_column(Boolean, default=False)
    inicio: Mapped[int] = mapped_column(Integer)
    fim: Mapped[int | None] = mapped_column(Integer)
    controle_id: Mapped[int | None] = mapped_column(ForeignKey("controles.id", ondelete="SET NULL"))
    controle_valor: Mapped[int] = mapped_column(Integer, default=0)
    controle_expr: Mapped[str] = mapped_column(String(255), default="")
    ordem: Mapped[int] = mapped_column(Integer, default=0)

    conta: Mapped[Conta | None] = relationship()
    controle: Mapped[Controle | None] = relationship()

    def ativa_em(self, comp: int) -> bool:
        return self.inicio <= comp and (self.fim is None or comp <= self.fim)


class Parcelada(Base):
    """Compra parcelada; gera um lançamento por parcela nos meses seguintes."""

    __tablename__ = "parceladas"

    id: Mapped[int] = mapped_column(primary_key=True)
    descricao: Mapped[str] = mapped_column(String(120))
    conta_id: Mapped[int] = mapped_column(ForeignKey("contas.id", ondelete="CASCADE"))
    valor_total: Mapped[int] = mapped_column(Integer)
    valor_expr: Mapped[str] = mapped_column(String(255), default="")
    parcelas: Mapped[int] = mapped_column(Integer)
    # Competência em que cai a parcela número 1 (pode ser no passado).
    primeira: Mapped[int] = mapped_column(Integer)
    # Primeira parcela que foi lançada no sistema (para compras já em andamento).
    lancada_desde: Mapped[int] = mapped_column(Integer, default=1)
    # Verba descontada por cada parcela (ex.: Limpeza). verba_valor positivo reduz
    # o que sai da verba; vazio = o valor da parcela com sinal invertido.
    verba_id: Mapped[int | None] = mapped_column(ForeignKey("recorrentes.id", ondelete="SET NULL"))
    verba_valor: Mapped[int | None] = mapped_column(Integer)
    verba_expr: Mapped[str] = mapped_column(String(255), default="")

    conta: Mapped[Conta] = relationship()
    verba: Mapped[Recorrente | None] = relationship()
    lancamentos: Mapped[list["Lancamento"]] = relationship(
        back_populates="parcelada", cascade="all, delete-orphan"
    )

    def valor_parcela(self, n: int) -> int:
        base = round(self.valor_total / self.parcelas)
        if n == self.parcelas:
            return self.valor_total - base * (self.parcelas - 1)
        return base


class Lancamento(Base):
    __tablename__ = "lancamentos"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("contas.id", ondelete="CASCADE"), index=True)
    comp: Mapped[int] = mapped_column(Integer, index=True)
    descricao: Mapped[str] = mapped_column(String(120))
    valor: Mapped[int] = mapped_column(Integer)
    valor_expr: Mapped[str] = mapped_column(String(255), default="")
    recorrente_id: Mapped[int | None] = mapped_column(ForeignKey("recorrentes.id", ondelete="SET NULL"))
    parcelada_id: Mapped[int | None] = mapped_column(ForeignKey("parceladas.id", ondelete="CASCADE"))
    parcela: Mapped[int | None] = mapped_column(Integer)
    # Gasto no cartão que consome uma verba (Recorrente com verba=True).
    verba_id: Mapped[int | None] = mapped_column(ForeignKey("recorrentes.id", ondelete="SET NULL"))
    # Quanto este gasto desconta da verba (positivo). Vazio = o valor com sinal invertido.
    verba_valor: Mapped[int | None] = mapped_column(Integer)
    eh_verba: Mapped[bool] = mapped_column(Boolean, default=False)
    # Marcação "verde" da planilha: recebido de fato / débito automático cadastrado.
    confirmado: Mapped[bool] = mapped_column(Boolean, default=False)
    ordem: Mapped[int] = mapped_column(Integer, default=0)

    conta: Mapped[Conta] = relationship()
    recorrente: Mapped[Recorrente | None] = relationship(foreign_keys=[recorrente_id])
    verba: Mapped[Recorrente | None] = relationship(foreign_keys=[verba_id])

    @property
    def desconto_verba(self) -> int:
        return -self.valor if self.verba_valor is None else self.verba_valor
    parcelada: Mapped[Parcelada | None] = relationship(back_populates="lancamentos")


class Movimento(Base):
    __tablename__ = "movimentos"

    id: Mapped[int] = mapped_column(primary_key=True)
    controle_id: Mapped[int] = mapped_column(ForeignKey("controles.id", ondelete="CASCADE"), index=True)
    comp: Mapped[int] = mapped_column(Integer, index=True)
    descricao: Mapped[str] = mapped_column(String(120))
    valor: Mapped[int] = mapped_column(Integer)
    valor_expr: Mapped[str] = mapped_column(String(255), default="")
    recorrente_id: Mapped[int | None] = mapped_column(ForeignKey("recorrentes.id", ondelete="SET NULL"))

    controle: Mapped[Controle] = relationship(back_populates="movimentos")


class FaturaConfirmada(Base):
    """Fatura de um cartão num mês marcada como confirmada (paga/débito cadastrado)."""

    __tablename__ = "faturas_confirmadas"

    conta_id: Mapped[int] = mapped_column(ForeignKey("contas.id", ondelete="CASCADE"), primary_key=True)
    comp: Mapped[int] = mapped_column(Integer, primary_key=True)


class MesGerado(Base):
    """Meses em que os recorrentes já foram lançados."""

    __tablename__ = "meses_gerados"
    comp: Mapped[int] = mapped_column(Integer, primary_key=True)
