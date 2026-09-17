"""Comprar dólar: uma transferência com valores diferentes em cada ponta.

Até aqui um par de transferência interna era espelhado -- o mesmo
`entry_amount` nas duas pontas --, e isso estava certo enquanto todas as contas
eram em real. Entre moedas diferentes o espelho vira defeito: gravar o valor
em reais também na conta em dólar é escrever um número que nunca existiu no
extrato dela.

A regra medida aqui: mesma moeda continua espelhada; moedas diferentes exigem
o valor de cada ponta, recusam parcelamento e recorrência (cada compra tem a
taxa do seu dia) e realizam cada lado pelo próprio valor -- inclusive quando a
realização vem da conciliação de extrato, que é por onde um valor em real
entraria calado numa conta em dólar.

A taxa efetiva não é gravada de propósito: é a divisão de uma ponta pela outra.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from bank_statements.models import BankStatementImport, BankStatementLine
from bank_statements.reconciliation import reconcile_line_with_entry
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    CURRENCY_BRL,
    CURRENCY_USD,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SCOPE_ALL,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from transactions import services
from transactions.models import CashFlowCategory, CashFlowEntry
from transactions.operations import operations_page_for_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario():
    """Uma conta em real, uma em dólar, e o usuário com as duas concessões."""
    user = AppUser.objects.create_user(username="operador-cambio", password="senha-segura")
    owner = AccountOwner.objects.create(name="Titular")
    banco = FinancialInstitution.objects.create(
        institution_name="Banco dos testes", institution_type="Banco"
    )
    corretora = FinancialInstitution.objects.create(
        institution_name="Avenue dos testes", institution_type="Corretora"
    )
    real = FinancialAccount.objects.create(
        owner=owner, institution=banco, account_name="Conta corrente", currency=CURRENCY_BRL
    )
    dolar = FinancialAccount.objects.create(
        owner=owner, institution=corretora, account_name="Avenue", currency=CURRENCY_USD
    )
    outra_em_real = FinancialAccount.objects.create(
        owner=owner, institution=banco, account_name="Poupança", currency=CURRENCY_BRL
    )
    UserOwnerAccess.objects.create(
        user=user, owner=owner, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    save_transfer_destination_accesses(user, {dolar.id, outra_em_real.id})
    categoria = CashFlowCategory.objects.create(category_name="Transferência", kind=CATEGORY_KIND_TRANSFER)
    return user, real, dolar, outra_em_real, categoria


def _requisicao(origem, destino, categoria, **extras):
    campos = {
        "account_id": origem.id,
        "category_id": categoria.id,
        "entry_type": ENTRY_TYPE_EXPENSE,
        "description": "Compra de dólar",
        "entry_amount": Decimal("5400.00"),
        "installments": 1,
        "due_date": date(2026, 9, 10),
        "status": STATUS_PROJECTED,
        "counterparty_account_id": destino.id,
    }
    campos.update(extras)
    return services.TransactionRequest(**campos)


# --- As duas pontas --------------------------------------------------------


def test_par_em_moedas_diferentes_guarda_um_valor_em_cada_ponta(cenario):
    user, real, dolar, _outra, categoria = cenario

    origem, destino = services.create_transaction_batch(
        _requisicao(real, dolar, categoria, counterparty_amount=Decimal("1000.00")), user=user
    )

    assert origem.account_id == real.id
    assert origem.entry_amount == Decimal("5400.00")
    assert destino.account_id == dolar.id
    assert destino.entry_amount == Decimal("1000.00")
    # A taxa efetiva é derivada, nunca gravada: 5400 / 1000.
    assert origem.entry_amount / destino.entry_amount == Decimal("5.4")


def test_par_na_mesma_moeda_continua_espelhado(cenario):
    """A invariante antiga não pode ter sido afrouxada pela nova regra."""
    user, real, _dolar, outra_em_real, categoria = cenario

    origem, destino = services.create_transaction_batch(
        _requisicao(real, outra_em_real, categoria), user=user
    )

    assert origem.entry_amount == destino.entry_amount == Decimal("5400.00")


def test_valor_do_destino_e_obrigatorio_quando_as_moedas_diferem(cenario):
    """Sem ele o sistema teria de inventar uma taxa -- e nenhuma seria auditável."""
    user, real, dolar, _outra, categoria = cenario

    with pytest.raises(ValueError, match="quanto foi creditado na conta destino"):
        services.create_transaction_batch(_requisicao(real, dolar, categoria), user=user)

    assert CashFlowEntry.objects.count() == 0


def test_valor_do_destino_e_recusado_quando_as_moedas_sao_iguais(cenario):
    """Dois valores na mesma moeda seriam duas verdades sobre a mesma quantia."""
    user, real, _dolar, outra_em_real, categoria = cenario

    with pytest.raises(ValueError, match="mesma moeda"):
        services.create_transaction_batch(
            _requisicao(real, outra_em_real, categoria, counterparty_amount=Decimal("5400.00")),
            user=user,
        )

    assert CashFlowEntry.objects.count() == 0


@pytest.mark.parametrize("valor", [Decimal("0.00"), Decimal("-1000.00")])
def test_valor_do_destino_precisa_ser_positivo(cenario, valor):
    user, real, dolar, _outra, categoria = cenario

    with pytest.raises(ValueError, match="positivo"):
        services.create_transaction_batch(
            _requisicao(real, dolar, categoria, counterparty_amount=valor), user=user
        )

    assert CashFlowEntry.objects.count() == 0


@pytest.mark.parametrize(
    "extras",
    [
        {"installments": 3},
        {"is_recurring": True},
    ],
    ids=["parcelado", "recorrente"],
)
def test_transferencia_entre_moedas_recusa_repeticao(cenario, extras):
    """Cada compra de moeda tem a taxa do seu dia; repetir gravaria ficção."""
    user, real, dolar, _outra, categoria = cenario

    with pytest.raises(ValueError, match="parcelamento nem recorrência"):
        services.create_transaction_batch(
            _requisicao(real, dolar, categoria, counterparty_amount=Decimal("1000.00"), **extras),
            user=user,
        )

    assert CashFlowEntry.objects.count() == 0


def test_transferencia_na_mesma_moeda_continua_aceitando_parcelamento(cenario):
    """A recusa é da mistura de moedas, não do parcelamento."""
    user, real, _dolar, outra_em_real, categoria = cenario

    lancamentos = services.create_transaction_batch(
        _requisicao(real, outra_em_real, categoria, installments=3), user=user
    )

    assert len(lancamentos) == 6
    assert {e.entry_amount for e in lancamentos} == {Decimal("5400.00")}


# --- Realização ------------------------------------------------------------


def test_realizar_uma_ponta_nao_leva_o_valor_dela_para_a_outra_moeda(cenario):
    """O teste que mais importa: o valor em real não pode virar valor em dólar."""
    user, real, dolar, _outra, categoria = cenario
    origem, destino = services.create_transaction_batch(
        _requisicao(real, dolar, categoria, counterparty_amount=Decimal("1000.00")), user=user
    )

    services.realize_transaction(
        origem, realized_date=date(2026, 9, 10), realized_amount=Decimal("5450.00"), user=user
    )

    origem.refresh_from_db()
    destino.refresh_from_db()
    assert origem.realized_amount == Decimal("5450.00")
    assert destino.status == STATUS_REALIZED
    assert destino.realized_amount == Decimal("1000.00")


def test_realizar_na_mesma_moeda_continua_espelhando_o_valor(cenario):
    user, real, _dolar, outra_em_real, categoria = cenario
    origem, destino = services.create_transaction_batch(
        _requisicao(real, outra_em_real, categoria), user=user
    )

    services.realize_transaction(
        origem, realized_date=date(2026, 9, 10), realized_amount=Decimal("5450.00"), user=user
    )

    destino.refresh_from_db()
    assert destino.realized_amount == Decimal("5450.00")


def test_conciliar_o_extrato_em_real_nao_escreve_reais_na_conta_em_dolar(cenario):
    """A conciliação realiza pelo valor da linha -- que é da conta dela, só."""
    user, real, dolar, _outra, categoria = cenario
    origem, destino = services.create_transaction_batch(
        _requisicao(real, dolar, categoria, counterparty_amount=Decimal("1000.00")), user=user
    )
    importacao = BankStatementImport.objects.create(account=real, source_filename="extrato.csv")
    linha = BankStatementLine.objects.create(
        import_batch=importacao,
        account=real,
        statement_date=date(2026, 9, 10),
        description="Compra de dólar",
        amount=Decimal("-5400.00"),
        line_hash="compra-de-dolar",
    )

    reconcile_line_with_entry(user, line_id=linha.id, entry_id=origem.id)

    origem.refresh_from_db()
    destino.refresh_from_db()
    assert origem.realized_amount == Decimal("5400.00")
    assert destino.realized_amount == Decimal("1000.00")


# --- Edição ----------------------------------------------------------------


def test_editar_o_par_atualiza_cada_ponta_com_o_seu_valor(cenario):
    user, real, dolar, _outra, categoria = cenario
    origem, destino = services.create_transaction_batch(
        _requisicao(real, dolar, categoria, counterparty_amount=Decimal("1000.00")), user=user
    )

    services.update_transaction_operation(
        origem,
        _requisicao(
            real, dolar, categoria,
            entry_amount=Decimal("5600.00"),
            counterparty_amount=Decimal("1020.00"),
        ),
        OPERATION_SCOPE_ALL,
        user=user,
    )

    origem.refresh_from_db()
    destino.refresh_from_db()
    assert origem.entry_amount == Decimal("5600.00")
    assert destino.entry_amount == Decimal("1020.00")


def test_editar_o_par_sem_o_valor_do_destino_e_recusado(cenario):
    """Omitir o valor na edição não pode ressuscitar o espelho."""
    user, real, dolar, _outra, categoria = cenario
    origem, destino = services.create_transaction_batch(
        _requisicao(real, dolar, categoria, counterparty_amount=Decimal("1000.00")), user=user
    )

    with pytest.raises(ValueError, match="quanto foi creditado na conta destino"):
        services.update_transaction_operation(
            origem,
            _requisicao(real, dolar, categoria, entry_amount=Decimal("5600.00")),
            OPERATION_SCOPE_ALL,
            user=user,
        )

    destino.refresh_from_db()
    assert destino.entry_amount == Decimal("1000.00")


def test_converter_lancamento_simples_em_transferencia_entre_moedas(cenario):
    user, real, dolar, _outra, categoria = cenario
    comum = CashFlowCategory.objects.create(category_name="Comum")
    lancamento = CashFlowEntry.objects.create(
        account=real,
        category=comum,
        entry_type=ENTRY_TYPE_EXPENSE,
        description="A classificar",
        entry_amount=Decimal("5400.00"),
        due_date=date(2026, 9, 10),
        status=STATUS_PROJECTED,
    )

    origem, destino = services.update_transaction_operation(
        lancamento,
        _requisicao(real, dolar, categoria, counterparty_amount=Decimal("1000.00")),
        user=user,
    )

    assert origem.entry_amount == Decimal("5400.00")
    assert destino.account_id == dolar.id
    assert destino.entry_amount == Decimal("1000.00")


# --- Apresentação ----------------------------------------------------------


def test_a_operacao_vale_o_valor_da_origem_na_moeda_da_origem(cenario):
    """Antes o total do par era o maior dos dois valores. Entre moedas, o maior
    é só o de número maior -- e sairia rotulado com a moeda errada."""
    user, real, dolar, _outra, categoria = cenario
    services.create_transaction_batch(
        _requisicao(real, dolar, categoria, counterparty_amount=Decimal("1000.00")), user=user
    )

    pagina = operations_page_for_user(user)

    assert len(pagina.operations) == 1
    operacao = pagina.operations[0]
    assert operacao.total_amount == Decimal("5400.00")
    assert operacao.total_currency == CURRENCY_BRL


def test_a_linha_de_edicao_so_reapresenta_o_valor_da_outra_ponta_entre_moedas(cenario):
    """Em moeda igual o campo nem aparece na tela, e vir preenchido o faria
    aparecer com um valor que o serviço recusaria."""
    user, real, dolar, outra_em_real, categoria = cenario
    # Vencimento hoje: o status de um lançamento depende da data em que a suíte
    # roda, e uma data fixa sairia do modo "a vencer" assim que passasse.
    hoje = date.today()
    entre_moedas, _destino_usd = services.create_transaction_batch(
        _requisicao(real, dolar, categoria, due_date=hoje, counterparty_amount=Decimal("1000.00")),
        user=user,
    )
    mesma_moeda, _destino_brl = services.create_transaction_batch(
        _requisicao(real, outra_em_real, categoria, due_date=hoje), user=user
    )

    # Filtrado na conta em real de propósito: sem recorte, a seleção
    # atravessaria moedas e a etapa anterior já a recusaria (`MixedCurrencyError`).
    contexto = services.build_transactions_view_context(
        user, {"account_id": str(real.id)}, {}, request=None
    )
    valores = {t.id: t.counterparty_entry_amount for t in contexto["txs"]}

    assert valores[entre_moedas.id] == Decimal("1000.00")
    assert valores[mesma_moeda.id] is None
