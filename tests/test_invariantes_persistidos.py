"""Invariantes que só um banco de verdade consegue provar.

POR QUE ESTE ARQUIVO EXISTE, E POR QUE ELE NÃO SUBSTITUI OS OUTROS

O resto da suíte roda sem banco, e isso é desenho consciente (ver
`conftest.py`): cabeçalhos, negação por padrão, CSRF e autorização são
decididos antes de qualquer consulta, e mantê-los sem infraestrutura faz a
suíte caber em segundos.

Só que três coisas que o `AGENTS.md` declara como invariante ficavam de fora
por construção, e não por descuido:

1. **`@transaction.atomic` desfaz de verdade.** `test_financial_mutations.py`
   chama `services.close_month.__wrapped__` -- o `__wrapped__` DESEMBRULHA o
   decorador. É a escolha certa lá, porque aquele teste mede autorização; mas
   significa que nenhum teste jamais exercitou a transação em si.
2. **As `CheckConstraint` e a `UniqueConstraint` são o piso.** Elas vivem no
   PostgreSQL. Um `Mock` aceita `entry_amount=0` sem reclamar; o banco não.
3. **A operação composta não deixa estado parcial.** Só dá para observar isso
   contando linhas depois de uma falha real.

Estes testes cobrem essas três, e só essas. Eles são o piso da fase F1 do
`LEVANTAMENTO_2026-09.md`, não uma tentativa de cobertura geral.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.utils import DataError

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ENTRY_TYPE_EXPENSE,
    OPERATION_SINGLE,
    STATUS_PROJECTED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from transactions import services
from transactions.models import AccountMonthClose, CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Cenário mínimo. Objetos reais, não dublês: o ponto do arquivo é justamente
# fazer o PostgreSQL participar.
# ---------------------------------------------------------------------------


@pytest.fixture
def usuario():
    # `administrator` recebe escopo amplo de dados (ver `_has_broad_owner_access`
    # em `accounts/services.py`), o que dispensa montar `UserOwnerAccess` aqui.
    # Autorização já é medida em `test_authorization.py`; aqui ela só não pode
    # atrapalhar.
    return get_user_model().objects.create_user(
        username="teste-invariantes",
        password="troca-esta-senha-no-primeiro-acesso",
        user_type=USER_TYPE_ADMINISTRATOR,
    )


@pytest.fixture
def conta():
    titular = AccountOwner.objects.create(name="Titular de teste")
    instituicao = FinancialInstitution.objects.create(
        institution_name="Banco de teste",
        # "Banco", com maiúscula: a `CheckConstraint`
        # `ck_financial_institution_type_valid` aceita só `Banco` e `Corretora`.
        # A primeira versão desta fixture usava minúscula e foi recusada pelo
        # banco -- o que, para um arquivo cujo assunto é justamente esse piso,
        # é uma demonstração conveniente.
        institution_type="Banco",
    )
    return FinancialAccount.objects.create(
        owner=titular,
        institution=instituicao,
        account_name="Conta corrente de teste",
        initial_balance=Decimal("1000.00"),
    )


@pytest.fixture
def categoria():
    return CashFlowCategory.objects.create(category_name="Categoria de teste")


def _lancamento(conta, categoria, **campos):
    padrao = {
        "account": conta,
        "category": categoria,
        "entry_type": ENTRY_TYPE_EXPENSE,
        "description": "Lançamento de teste",
        "entry_amount": Decimal("100.00"),
        "due_date": date(2026, 6, 10),
        "status": STATUS_PROJECTED,
        "operation_type": OPERATION_SINGLE,
    }
    padrao.update(campos)
    return CashFlowEntry.objects.create(**padrao)


# ---------------------------------------------------------------------------
# 1. As CheckConstraint existem no banco e recusam o que prometem recusar
# ---------------------------------------------------------------------------


def test_banco_recusa_lancamento_com_valor_zero(conta, categoria):
    """Invariante: "lançamentos armazenam valores positivos".

    A regra também é validada na camada de aplicação. Este teste mede a OUTRA
    metade: que existe um piso no banco para quando a aplicação for contornada
    -- carga de dados, correção manual, um caminho de código novo que esqueça a
    validação.
    """
    # O `transaction.atomic()` ao lado do `raises` não é enfeite: um
    # `IntegrityError` quebra a transação do teste, e sem o savepoint que ele
    # abre todas as consultas seguintes falhariam com "current transaction is
    # aborted". Vale para todos os testes de constraint deste arquivo.
    with pytest.raises(IntegrityError, match="ck_cash_flow_entry_amount_positive"), transaction.atomic():
        _lancamento(conta, categoria, entry_amount=Decimal("0.00"))

    assert CashFlowEntry.objects.count() == 0


def test_banco_recusa_lancamento_com_valor_negativo(conta, categoria):
    with pytest.raises(IntegrityError, match="ck_cash_flow_entry_amount_positive"), transaction.atomic():
        _lancamento(conta, categoria, entry_amount=Decimal("-1.00"))

    assert CashFlowEntry.objects.count() == 0


def test_banco_recusa_status_fora_do_vocabulario(conta, categoria):
    """Invariante: "status são `a_vencer`, `vencidos` e `realizado`".

    `core.domain.finance` é a fonte única do vocabulário, e a
    `CheckConstraint` é o que impede uma quarta palavra de existir na tabela.
    """
    with pytest.raises(IntegrityError, match="ck_cash_flow_entry_status_valid"), transaction.atomic():
        _lancamento(conta, categoria, status="cancelado")

    assert CashFlowEntry.objects.count() == 0


def test_banco_recusa_valor_realizado_nao_positivo(conta, categoria):
    match = "ck_cash_flow_entry_realized_amount_positive"
    with pytest.raises(IntegrityError, match=match), transaction.atomic():
        _lancamento(conta, categoria, realized_amount=Decimal("0.00"))


# ---------------------------------------------------------------------------
# 2. O fechamento mensal é único por conta e período
# ---------------------------------------------------------------------------


def test_fechamento_mensal_e_unico_por_conta_e_periodo(conta, usuario):
    """Invariante: "fechamento mensal bloqueia mutações do período".

    `close_month` recusa o segundo fechamento com `ValueError`, e isso já é
    medido sem banco. O que só o banco prova é que a `UniqueConstraint`
    sustenta a regra mesmo quando o caminho da aplicação não é usado -- duas
    requisições simultâneas, por exemplo, podem passar juntas pelo
    `is_month_closed` antes de qualquer uma gravar.
    """
    services.close_month(conta, 2026, 6, Decimal("500.00"), usuario)

    # Pela aplicação: recusa explicada.
    with pytest.raises(ValueError, match="já está fechado"):
        services.close_month(conta, 2026, 6, Decimal("500.00"), usuario)

    # Por baixo dela: o banco também recusa, e é ele o piso.
    with pytest.raises(IntegrityError, match="uq_account_month_close_account_period"), transaction.atomic():
        AccountMonthClose.objects.create(
            account=conta,
            year=2026,
            month=6,
            closing_balance=Decimal("500.00"),
            closed_at=services.timezone.now(),
            closed_by_user=usuario,
        )

    assert AccountMonthClose.objects.filter(account=conta, year=2026, month=6).count() == 1


def test_banco_recusa_mes_fora_da_faixa(conta, usuario):
    with pytest.raises(IntegrityError, match="ck_account_month_close_month_range"), transaction.atomic():
        AccountMonthClose.objects.create(
            account=conta,
            year=2026,
            month=13,
            closing_balance=Decimal("0.00"),
            closed_at=services.timezone.now(),
            closed_by_user=usuario,
        )


# ---------------------------------------------------------------------------
# 3. Atomicidade: a falha no meio não deixa metade gravada
# ---------------------------------------------------------------------------


def test_close_month_nao_deixa_fechamento_orfao_quando_a_auditoria_falha(
    conta, usuario, monkeypatch
):
    """Invariante: "operações compostas são atômicas e services delimitam
    transações".

    Este é o teste que o `__wrapped__` de `test_financial_mutations.py` não
    pode fazer. `close_month` grava o `AccountMonthClose` e SÓ DEPOIS registra
    a auditoria. Se o segundo passo falhar e a transação não desfizer o
    primeiro, sobra um mês fechado sem rastro de quem o fechou -- e o mês
    passa a bloquear mutações sem que ninguém consiga explicar por quê.

    Forçamos a falha exatamente nessa fresta e contamos as linhas.
    """

    def auditoria_quebrada(*args, **kwargs):
        raise RuntimeError("falha simulada depois da gravação")

    import core.services

    monkeypatch.setattr(core.services, "log_audit_event", auditoria_quebrada)

    with pytest.raises(RuntimeError, match="falha simulada"):
        services.close_month(conta, 2026, 7, Decimal("900.00"), usuario)

    assert AccountMonthClose.objects.filter(account=conta, year=2026, month=7).count() == 0, (
        "O fechamento sobreviveu à falha da auditoria: a transação do service "
        "não está cobrindo a operação inteira."
    )


def test_transacao_desfeita_nao_deixa_lancamento(conta, categoria):
    """A mesma garantia, no nível mais cru possível.

    Serve de controle: se este teste falhar, o problema não é do domínio -- é
    da configuração da suíte, e nenhum dos outros resultados deste arquivo
    significa coisa alguma.
    """
    with pytest.raises(RuntimeError), transaction.atomic():
        _lancamento(conta, categoria, description="Some depois do erro")
        assert CashFlowEntry.objects.count() == 1
        raise RuntimeError("desfaz")

    assert CashFlowEntry.objects.count() == 0


# ---------------------------------------------------------------------------
# 4. Decimal, e não float
# ---------------------------------------------------------------------------


def test_valor_volta_do_banco_como_decimal_exato(conta, categoria):
    """Invariante: "valores financeiros usam `Decimal`".

    A ida até o banco e a volta é o único jeito de provar que a coluna é
    `numeric` e não `double precision`. Com float, `0.1 + 0.2` guardado e lido
    de volta não fecha; com `numeric`, fecha.
    """
    _lancamento(conta, categoria, entry_amount=Decimal("0.10"))
    _lancamento(conta, categoria, entry_amount=Decimal("0.20"))

    total = sum(e.entry_amount for e in CashFlowEntry.objects.all())

    assert isinstance(total, Decimal)
    assert total == Decimal("0.30")
    assert str(total) == "0.30"


def test_banco_recusa_valor_acima_da_precisao_da_coluna(conta, categoria):
    """`max_digits=12, decimal_places=2` é contrato, não sugestão."""
    with pytest.raises((DataError, IntegrityError)), transaction.atomic():
        _lancamento(conta, categoria, entry_amount=Decimal("12345678901.00"))
