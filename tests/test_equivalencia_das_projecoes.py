"""A projeção da tela e a v3/projection contam o mesmo saldo no fim de cada mês.

O CB calcula a projeção de caixa duas vezes: a tela usa
`reports.services.projection_months_between`, e a rota `v3/projection` usa
`core.patrimonio.montar_projecao`, com os filtros escritos à parte. A análise de
arquitetura de 02/10/2026 pediu este teste ANTES de qualquer refatoração: se as
duas concordam, unificá-las é arrumação sem urgência; se discordam, uma está
errada, e o consolidador mostraria um número diferente do que a tela mostra.

O que se compara é o saldo no FIM de cada mês, por moeda. As bases são
diferentes de propósito:

- a tela parte do livro completo (realizados pela data de realização, os demais
  pela de vencimento) e soma mês a mês;
- a v3 parte do saldo REALIZADO de hoje, põe o vencido em aberto no dia-base e
  soma os lançamentos a vencer pela data de vencimento.

Para meses a partir de hoje elas têm de coincidir. O cenário tem de exercitar o
que poderia separá-las: lançamento realizado, vencido em aberto (no mês corrente
e em meses passados), a vencer, recorrência, transferência entre contas,
aporte em investimento, cartão com saldo negativo e uma segunda moeda.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core import patrimonio
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_RECURRING,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_ALL,
)
from reports import services
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

HOJE = date(2026, 6, 15)
FIM = date(2026, 12, 31)


@pytest.fixture
def cenario():
    titular = AccountOwner.objects.create(name="Titular da projeção")
    banco = FinancialInstitution.objects.create(institution_name="Banco da projeção", institution_type="Banco")
    corretora = FinancialInstitution.objects.create(institution_name="Corretora da projeção", institution_type="Corretora")

    def conta(nome, saldo, moeda="BRL", instituicao=banco, **extras):
        return FinancialAccount.objects.create(
            owner=titular, institution=instituicao, account_name=nome, initial_balance=Decimal(saldo),
            currency=moeda, initial_balance_date=date(2025, 12, 31), **extras,
        )

    corrente = conta("Corrente", "1000.00")
    poupanca = conta("Poupança", "250.50")
    cartao = conta("Cartão", "-300.00", account_kind=ACCOUNT_KIND_CREDIT_CARD, card_closing_day=10, card_due_day=17)
    dolar = conta("Dólar", "500.00", "USD", corretora)
    gerencial = CashFlowCategory.objects.create(category_name="Gerencial", kind=CATEGORY_KIND_MANAGERIAL)
    transferencia = CashFlowCategory.objects.create(category_name="Transferência", kind=CATEGORY_KIND_TRANSFER)
    movimentacao = CashFlowCategory.objects.create(category_name="Aporte", kind=CATEGORY_KIND_MOVEMENT)

    def lancar(conta_, categoria, tipo, valor, vencimento, *, realizado=None, **extras):
        return CashFlowEntry.objects.create(
            account=conta_, category=categoria, entry_type=tipo, description=f"{tipo} {valor} {vencimento}",
            entry_amount=Decimal(valor), due_date=vencimento,
            status=STATUS_REALIZED if realizado else extras.pop("status", STATUS_PROJECTED),
            realized_date=vencimento if realizado else None,
            realized_amount=Decimal(realizado) if realizado else None, **extras,
        )

    # Realizados no passado.
    lancar(corrente, gerencial, ENTRY_TYPE_INCOME, "5000.00", date(2026, 1, 5), realizado="5000.00")
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "1200.00", date(2026, 2, 8), realizado="1187.35")
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "80.00", date(2026, 6, 3), realizado="80.00")
    lancar(dolar, gerencial, ENTRY_TYPE_INCOME, "40.00", date(2026, 5, 20), realizado="40.00")
    # Vencidos em aberto: num mês passado e no mês corrente antes de hoje.
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "99.90", date(2026, 3, 12))
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "45.10", date(2026, 6, 10), status=STATUS_PENDING)
    lancar(poupanca, gerencial, ENTRY_TYPE_INCOME, "12.34", date(2026, 6, 1))
    # A vencer: hoje, mês corrente, meses futuros, nas duas moedas e no cartão.
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "300.00", HOJE)
    lancar(corrente, gerencial, ENTRY_TYPE_INCOME, "5000.00", date(2026, 7, 5))
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "1500.00", date(2026, 7, 20))
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "210.45", date(2026, 9, 9))
    lancar(dolar, gerencial, ENTRY_TYPE_EXPENSE, "15.00", date(2026, 8, 14))
    lancar(cartao, gerencial, ENTRY_TYPE_EXPENSE, "620.80", date(2026, 7, 25))
    lancar(corrente, movimentacao, ENTRY_TYPE_EXPENSE, "700.00", date(2026, 8, 3))
    lancar(corrente, movimentacao, ENTRY_TYPE_INCOME, "150.00", date(2026, 11, 3))
    # Recorrência: o mesmo aluguel nos meses futuros.
    for mes in (7, 8, 9, 10, 11, 12):
        lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "1800.00", date(2026, mes, 10),
               operation_type=OPERATION_RECURRING, is_recurring=True)
    # Transferência entre duas contas da mesma moeda: líquido zero na moeda.
    from django.db import transaction

    from core.domain.finance import OPERATION_INTERNAL_TRANSFER
    from transactions.models import BankOperation

    operacao = BankOperation.objects.create(operation_key="transferencia-da-projecao", operation_type=OPERATION_INTERNAL_TRANSFER)
    with transaction.atomic():
        origem = CashFlowEntry.objects.create(
            account=corrente, category=transferencia, entry_type=ENTRY_TYPE_EXPENSE, description="Para a poupança",
            entry_amount=Decimal("400.00"), due_date=date(2026, 8, 25), status=STATUS_PROJECTED,
            operation_type=OPERATION_INTERNAL_TRANSFER, bank_operation=operacao,
        )
        CashFlowEntry.objects.create(
            account=poupanca, category=transferencia, entry_type=ENTRY_TYPE_INCOME, description="Da corrente",
            entry_amount=Decimal("400.00"), due_date=date(2026, 8, 25), status=STATUS_PROJECTED,
            operation_type=OPERATION_INTERNAL_TRANSFER, bank_operation=operacao, source_entry=origem,
        )
    return {"BRL": [corrente, poupanca, cartao], "USD": [dolar]}


def _saldos_da_v3() -> dict[tuple[str, str], str]:
    corpo = patrimonio.montar_projecao(HOJE, FIM)
    return {(linha["mes"], linha["moeda"]): linha["saldo_final"] for linha in corpo["meses"]}


@pytest.mark.parametrize("moeda", ["BRL", "USD"])
def test_o_saldo_de_fim_de_mes_e_o_mesmo_na_tela_e_na_v3(cenario, moeda):
    tela = services.projection_months_between(
        [conta.id for conta in cenario[moeda]], date(2026, 6, 1), date(2026, 12, 1), VIEW_ALL
    )
    v3 = _saldos_da_v3()

    assert len(tela) == 7  # junho a dezembro
    diferencas = []
    for mes in tela:
        chave = mes["month"]
        esperado = str(mes["saldo"].quantize(Decimal("0.01")))
        obtido = v3[(chave, moeda)]
        if esperado != obtido:
            diferencas.append(f"{chave} {moeda}: tela={esperado} v3={obtido}")
    assert not diferencas, "as projeções divergem:\n  " + "\n  ".join(diferencas)
