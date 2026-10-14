"""Valores monetários em centavos (int) e avaliação de expressões como na planilha.

Aceita entradas como "-156,98/2", "-175-53.9-(-19.9-10)" ou "1.234,56".
"""

import ast
import operator
import re
from fractions import Fraction

MESES = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


class ExpressaoInvalida(ValueError):
    pass


def _normalizar(expr: str) -> str:
    expr = expr.strip().lstrip("=").replace("R$", "").replace(" ", "")
    if "," in expr:
        # Formato brasileiro: ponto é milhar, vírgula é decimal.
        expr = re.sub(r"(?<=\d)\.(?=\d{3}(\D|$))", "", expr).replace(",", ".")
    return expr


def _eval(no: ast.AST) -> Fraction:
    if isinstance(no, ast.Expression):
        return _eval(no.body)
    if isinstance(no, ast.Constant) and isinstance(no.value, (int, float)):
        return Fraction(str(no.value))
    if isinstance(no, ast.UnaryOp) and isinstance(no.op, (ast.USub, ast.UAdd)):
        v = _eval(no.operand)
        return -v if isinstance(no.op, ast.USub) else v
    if isinstance(no, ast.BinOp) and type(no.op) in _OPS:
        esq, dir_ = _eval(no.left), _eval(no.right)
        if isinstance(no.op, ast.Div) and dir_ == 0:
            raise ExpressaoInvalida("divisão por zero")
        return _OPS[type(no.op)](esq, dir_)
    raise ExpressaoInvalida("use apenas números, + - * / e parênteses")


def avaliar(expr: str) -> int:
    """Avalia a expressão e devolve o valor em centavos (arredondado)."""
    texto = _normalizar(expr or "")
    if not texto:
        raise ExpressaoInvalida("valor vazio")
    try:
        arvore = ast.parse(texto, mode="eval")
    except SyntaxError as e:
        raise ExpressaoInvalida(f"expressão inválida: {expr}") from e
    return round(_eval(arvore) * 100)


def eh_expressao(expr: str) -> bool:
    texto = _normalizar(expr or "")
    return bool(re.search(r"\d[-+*/(]|[)*/]", texto))


def formatar(centavos: int | None, sinal: bool = False) -> str:
    if centavos is None:
        return ""
    neg = centavos < 0
    inteiro, cent = divmod(abs(centavos), 100)
    texto = f"{inteiro:,}".replace(",", ".") + f",{cent:02d}"
    if neg:
        return f"-{texto}"
    return f"+{texto}" if sinal and centavos > 0 else texto


def para_entrada(centavos: int) -> str:
    """Representação editável (ex.: -78,49)."""
    neg = centavos < 0
    inteiro, cent = divmod(abs(centavos), 100)
    return f"{'-' if neg else ''}{inteiro},{cent:02d}"


# Competência = índice do mês: ano * 12 + (mes - 1).
def competencia(ano: int, mes: int) -> int:
    return ano * 12 + (mes - 1)


def ano_mes(comp: int) -> tuple[int, int]:
    return comp // 12, comp % 12 + 1


def nome_competencia(comp: int, curto: bool = False) -> str:
    ano, mes = ano_mes(comp)
    nome = MESES[mes - 1]
    return f"{nome[:3]}/{ano}" if curto else f"{nome} {ano}"
