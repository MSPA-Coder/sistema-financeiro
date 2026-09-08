"""A projeção recorrente não pode multiplicar o que já está duplicado.

O QUE ESTE ARQUIVO PROVA, E POR QUE O OUTRO NÃO PROVAVA

`test_projecao_recorrente_idempotente.py` mede a idempotência **dentro do
mês**: reexecutar não gera nada porque o horizonte não se move. Essa
propriedade é verdadeira -- e continua verdadeira enquanto a projeção duplica,
porque a duplicação acontece **entre** meses, no momento em que o horizonte
avança. Foi exatamente esse ponto cego que deixou o defeito passar: um
percurso manual rodou a projeção duas vezes seguidas, viu "0 gerados" na
segunda, e concluiu que não havia duplicação.

Havia. Em 08/09/2026, banco local e produção tinham 8 lançamentos a mais e
R$ 7.660,00 a mais no futuro, em quatro operações recorrentes.

O MECANISMO

`_extend_operation` toma como molde todas as linhas da maior data e
`_copy_occurrence` cria uma nova por molde. Isso está certo para as pernas de
uma transferência recorrente. Quando as linhas são iguais, porém, cada mês
seguinte nasce com o dobro -- e o seguinte com o dobro disso.

Estes testes tocam o banco de propósito: contar linhas depois de gerar é a
única forma de observar a propagação. Ver o docstring de
`test_invariantes_persistidos.py` sobre as duas camadas da suíte.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_RECURRING,
    STATUS_PENDING,
)
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry
from transactions.recurring_projection import _extend_operation

pytestmark = pytest.mark.django_db

HOJE = date(2026, 9, 8)
#: Último dia do mês seguinte ao da última ocorrência: dá exatamente um mês de
#: trabalho para a projeção, que é o suficiente para observar o que ela copia.
HORIZONTE = date(2026, 11, 30)
ULTIMA_DATA = date(2026, 10, 12)


@pytest.fixture
def cenario():
    titular = AccountOwner.objects.create(name="Titular da projeção")
    instituicao = FinancialInstitution.objects.create(
        # "Banco" com maiúscula: a `CheckConstraint` do banco só aceita
        # `Banco` e `Corretora`.
        institution_name="Banco da projeção",
        institution_type="Banco",
    )
    conta_a = FinancialAccount.objects.create(
        owner=titular,
        institution=instituicao,
        account_name="Conta A",
        initial_balance=Decimal("0.00"),
    )
    conta_b = FinancialAccount.objects.create(
        owner=titular,
        institution=instituicao,
        account_name="Conta B",
        initial_balance=Decimal("0.00"),
    )
    categoria = CashFlowCategory.objects.create(category_name="Categoria da projeção")
    operacao = BankOperation.objects.create(
        operation_key="teste-projecao-nao-duplica",
        operation_type=OPERATION_RECURRING,
        description="Recorrência de teste",
    )
    return conta_a, conta_b, categoria, operacao


def _ocorrencia(conta, categoria, operacao, **campos):
    padrao = {
        "account": conta,
        "category": categoria,
        "bank_operation": operacao,
        "entry_type": ENTRY_TYPE_INCOME,
        "description": "Aluguel de teste",
        "entry_amount": Decimal("990.00"),
        "due_date": ULTIMA_DATA,
        "status": STATUS_PENDING,
        "is_recurring": True,
        "operation_type": OPERATION_RECURRING,
    }
    padrao.update(campos)
    return CashFlowEntry.objects.create(**padrao)


def _geradas_em_novembro(operacao):
    return CashFlowEntry.objects.filter(
        bank_operation=operacao, due_date__year=2026, due_date__month=11
    )


def test_serie_normal_gera_uma_ocorrencia(cenario):
    """Controle positivo: sem ele, tudo abaixo passaria com a projeção morta."""
    conta_a, _conta_b, categoria, operacao = cenario
    _ocorrencia(conta_a, categoria, operacao)

    geradas = _extend_operation(
        list(CashFlowEntry.objects.filter(bank_operation=operacao)), HORIZONTE, HOJE
    )

    assert geradas == 1
    assert _geradas_em_novembro(operacao).count() == 1


def test_molde_duplicado_nao_se_propaga(cenario):
    """O defeito: duas linhas iguais no molde geravam duas em cada mês novo.

    Reproduz o estado real encontrado em produção -- duas linhas idênticas na
    mesma data, mesma operação, `source_entry_id` nulo nas duas.
    """
    conta_a, _conta_b, categoria, operacao = cenario
    _ocorrencia(conta_a, categoria, operacao)
    _ocorrencia(conta_a, categoria, operacao)

    geradas = _extend_operation(
        list(CashFlowEntry.objects.filter(bank_operation=operacao)), HORIZONTE, HOJE
    )

    assert geradas == 1, (
        "a projeção copiou a duplicata em vez de reconhecê-la: cada mês novo "
        "nasceria com o dobro, e o seguinte com o dobro disso"
    )
    assert _geradas_em_novembro(operacao).count() == 1


def test_duplicata_quadrupla_tambem_colapsa(cenario):
    """Quatro iguais é o estágio seguinte da mesma bola de neve."""
    conta_a, _conta_b, categoria, operacao = cenario
    for _ in range(4):
        _ocorrencia(conta_a, categoria, operacao)

    assert (
        _extend_operation(
            list(CashFlowEntry.objects.filter(bank_operation=operacao)), HORIZONTE, HOJE
        )
        == 1
    )


def test_pernas_de_transferencia_continuam_duas(cenario):
    """A correção não pode achatar o caso legítimo de duas linhas.

    Uma transferência recorrente tem destino e origem na mesma data e na mesma
    operação. Elas diferem em conta, descrição e tipo, e é isso que as separa
    de uma duplicata.
    """
    conta_a, conta_b, categoria, operacao = cenario
    destino = _ocorrencia(
        conta_a,
        categoria,
        operacao,
        description="Conta Destino: Conta A",
        entry_type=ENTRY_TYPE_INCOME,
    )
    _ocorrencia(
        conta_b,
        categoria,
        operacao,
        description="Conta Origem: Conta B",
        entry_type=ENTRY_TYPE_EXPENSE,
        source_entry=destino,
    )

    geradas = _extend_operation(
        list(CashFlowEntry.objects.filter(bank_operation=operacao)), HORIZONTE, HOJE
    )

    assert geradas == 2, "as duas pernas da transferência têm de ser copiadas"
    novas = _geradas_em_novembro(operacao)
    assert novas.count() == 2
    assert novas.filter(source_entry__isnull=False).count() == 1, (
        "a perna de origem tem de continuar apontando para a de destino"
    )


def test_transferencia_duplicada_volta_a_duas_pernas(cenario):
    """O caso composto: transferência legítima que foi duplicada inteira.

    Quatro linhas no molde -- duas pernas, cada uma em dobro -- têm de gerar
    duas, e a religação por `source_entry` tem de sobreviver ao descarte.
    """
    conta_a, conta_b, categoria, operacao = cenario
    for _ in range(2):
        destino = _ocorrencia(
            conta_a,
            categoria,
            operacao,
            description="Conta Destino: Conta A",
            entry_type=ENTRY_TYPE_INCOME,
        )
        _ocorrencia(
            conta_b,
            categoria,
            operacao,
            description="Conta Origem: Conta B",
            entry_type=ENTRY_TYPE_EXPENSE,
            source_entry=destino,
        )

    geradas = _extend_operation(
        list(CashFlowEntry.objects.filter(bank_operation=operacao)), HORIZONTE, HOJE
    )

    assert geradas == 2
    novas = _geradas_em_novembro(operacao)
    assert novas.filter(source_entry__isnull=False).count() == 1, (
        "a origem apontava para a perna descartada; sem o remapeamento, a "
        "transferência gerada ficaria sem contraparte"
    )
