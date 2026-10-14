# Controle Financeiro

Substitui a planilha `artifacts/Despesas 2027.xlsx`: uma tela por mês com a conta corrente (Nuconta)
e o cartão (Nubank), sem precisar copiar abas nem atualizar parcelas na mão.

## Rodando

```bash
uv run controle-financeiro
```

Abra http://127.0.0.1:8000. O login padrão é **admin / admin**. O app pede para trocar a senha.

Os dados ficam em `~/.controle-financeiro/` (`financas.db` + `secret.key`). Faça backup dessa pasta.

| Variável | Padrão | Para quê |
|---|---|---|
| `CF_DATA_DIR` | `~/.controle-financeiro` | pasta do banco |
| `CF_HOST` / `CF_PORT` | `127.0.0.1` / `8000` | onde o servidor escuta |
| `CF_LOGIN_VIA_INGRESS` | `1` | `0` dispensa o login do app para acessos pelo Ingress do Home Assistant (vindos do proxy `172.30.32.2`); o acesso direto continua pedindo login |
| `CF_CARREGAR_SALDO_POSITIVO` | `0` | `1` leva o saldo positivo para o mês seguinte (na planilha só o negativo é levado) |

Testes: `uv run pytest`

## Como a planilha virou o sistema

| Na planilha | No sistema |
|---|---|
| Aba de cada mês | **Mês**: o mês é criado automaticamente ao ser aberto, já com os fixos lançados |
| Salário, TIM, Condomínio… repetidos em toda aba | **Fixos**: cadastrados uma vez, com início e fim opcionais |
| `=-156.98/2` | O campo de valor aceita contas: `-156,98/2`, `-175-53,9-(-19,9-10)` |
| `Lava louça 3/10` atualizado à mão | **Parcelados**: as parcelas caem sozinhas nos meses certos |
| `Nubank =F60` | A fatura do cartão entra sozinha como linha na conta que a paga |
| `Saldo do Mês Anterior =IF(…<0,…,0)` | O saldo negativo é levado para o mês seguinte |
| `Limpeza = -600 - Hortifruti` | **Verba**: gastos no cartão marcados com a verba são descontados dela |
| Bloco "Dívidas" (Fabiane) e aba Poupanças | **Dívidas & poupanças**: saldos acumulados. Um fixo pode lançar um valor no controle todo mês (ex.: a parte da Fabiane na conta de luz) |

Convenção de sinais igual à da planilha: negativo é saída, positivo é entrada.

## Home Assistant

O app respeita o cabeçalho `X-Ingress-Path`, então funciona atrás do Ingress. O `Dockerfile` usa `/data`
como pasta de dados, que é o volume persistente de um add-on. Ainda falta criar o `config.yaml` do add-on.

## Próximos passos

- Importar extrato e fatura do Nubank (CSV/OFX)
- Migrações de banco (Alembic) antes de mudar o esquema com dados reais
- Add-on do Home Assistant
