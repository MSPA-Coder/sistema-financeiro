"""O esquema `leitura` publica as regras do CB para quem lê o banco de fora.

O FinancasMCP lia as tabelas direto e reescrevia em SQL o status efetivo, o
saldo e o que é gasto estimado de fatura. Quando uma coluna sumia, quebrava em
produção; quando a regra mudava aqui, respondia com a antiga sem erro. Estas
views carregam a regra, e este arquivo prova duas coisas:

1. as views dão os números que o domínio dá, em cenários montados à mão;
2. o PostgreSQL recusa mexer numa coluna que uma view lê, de modo que a quebra
   de schema reprova a migração de quem mexeu, e não o leitor em produção.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import DatabaseError, connection, transaction

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_SINGLE,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

VIEWS_ESPERADAS = {
    "categoria",
    "conta",
    "extrato_importacao",
    "extrato_linha",
    "fechamento_mes",
    "lancamento",
    "lancamento_etiqueta",
    "lancamento_projeto",
    "operacao",
    "orcamento_mensal",
}


def _consulta(sql: str, parametros=None) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(sql, parametros)
        return cursor.fetchall()


def _hoje() -> date:
    # A data que o próprio banco usa, e não a do relógio desta máquina.
    return _consulta("SELECT leitura.hoje()")[0][0]


@pytest.fixture
def conta():
    titular = AccountOwner.objects.create(name="Titular de leitura")
    instituicao = FinancialInstitution.objects.create(
        institution_name="Banco de leitura", institution_type="Banco"
    )
    return FinancialAccount.objects.create(
        owner=titular,
        institution=instituicao,
        account_name="Conta de leitura",
        initial_balance=Decimal("1000.00"),
    )


@pytest.fixture
def categoria():
    return CashFlowCategory.objects.create(category_name="Categoria de leitura")


def _lancamento(conta, categoria, **campos):
    padrao = {
        "account": conta,
        "category": categoria,
        "entry_type": ENTRY_TYPE_EXPENSE,
        "description": "Lançamento de leitura",
        "entry_amount": Decimal("100.00"),
        "due_date": _hoje() + timedelta(days=5),
        "status": STATUS_PROJECTED,
        "operation_type": OPERATION_SINGLE,
    }
    padrao.update(campos)
    return CashFlowEntry.objects.create(**padrao)


def test_o_esquema_publica_todas_as_views():
    existentes = {
        linha[0]
        for linha in _consulta(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'leitura'"
        )
    }
    assert existentes == VIEWS_ESPERADAS


def test_toda_view_tem_comentario_para_quem_explora_o_esquema():
    sem_comentario = [
        linha[0]
        for linha in _consulta(
            """
            SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'leitura' AND c.relkind = 'v'
              AND obj_description(c.oid, 'pg_class') IS NULL
              AND c.relname NOT IN ('lancamento_etiqueta', 'lancamento_projeto')
            """
        )
    ]
    assert not sem_comentario, f"views sem COMMENT: {sem_comentario}"


def test_status_efetivo_deriva_da_data_e_nao_do_gravado(conta, categoria):
    hoje = _hoje()
    vencido_gravado_a_vencer = _lancamento(conta, categoria, due_date=hoje - timedelta(days=3))
    vence_hoje = _lancamento(conta, categoria, due_date=hoje)
    vence_amanha = _lancamento(conta, categoria, due_date=hoje + timedelta(days=1))
    vencido_gravado_vencido = _lancamento(
        conta, categoria, due_date=hoje - timedelta(days=9), status=STATUS_PENDING
    )
    pago = _lancamento(
        conta,
        categoria,
        due_date=hoje - timedelta(days=20),
        status=STATUS_REALIZED,
        realized_date=hoje - timedelta(days=19),
        realized_amount=Decimal("90.00"),
    )

    efetivo = dict(_consulta("SELECT id, status FROM leitura.lancamento"))
    assert efetivo[vencido_gravado_a_vencer.id] == "vencidos"
    assert efetivo[vence_hoje.id] == "a_vencer"
    assert efetivo[vence_amanha.id] == "a_vencer"
    assert efetivo[vencido_gravado_vencido.id] == "vencidos"
    assert efetivo[pago.id] == "realizado"

    gravado = dict(_consulta("SELECT id, status_gravado FROM leitura.lancamento"))
    assert gravado[vencido_gravado_a_vencer.id] == STATUS_PROJECTED


def test_data_e_valor_que_valem_para_realizado_e_em_aberto(conta, categoria):
    hoje = _hoje()
    pago = _lancamento(
        conta,
        categoria,
        due_date=hoje + timedelta(days=30),
        status=STATUS_REALIZED,
        realized_date=hoje - timedelta(days=1),
        realized_amount=Decimal("87.50"),
    )
    aberto = _lancamento(conta, categoria, entry_amount=Decimal("40.00"), due_date=hoje + timedelta(days=7))

    linhas = {
        linha[0]: linha[1:]
        for linha in _consulta("SELECT id, data, valor, valor_previsto FROM leitura.lancamento")
    }
    assert linhas[pago.id] == (hoje - timedelta(days=1), Decimal("87.50"), Decimal("100.00"))
    assert linhas[aberto.id] == (hoje + timedelta(days=7), Decimal("40.00"), Decimal("40.00"))


def test_os_tres_saldos_da_conta(conta, categoria):
    hoje = _hoje()
    # Realizados: receita de 500 e despesa de 120 (valor realizado, não o previsto).
    _lancamento(
        conta, categoria, entry_type=ENTRY_TYPE_INCOME, entry_amount=Decimal("500.00"),
        status=STATUS_REALIZED, realized_date=hoje - timedelta(days=10), realized_amount=Decimal("500.00"),
        due_date=hoje - timedelta(days=10),
    )
    _lancamento(
        conta, categoria, entry_amount=Decimal("100.00"), status=STATUS_REALIZED,
        realized_date=hoje - timedelta(days=2), realized_amount=Decimal("120.00"),
        due_date=hoje - timedelta(days=2),
    )
    # Em aberto: uma despesa já vencida (30) e uma receita futura (200).
    _lancamento(conta, categoria, entry_amount=Decimal("30.00"), due_date=hoje - timedelta(days=4))
    _lancamento(
        conta, categoria, entry_type=ENTRY_TYPE_INCOME, entry_amount=Decimal("200.00"),
        due_date=hoje + timedelta(days=15),
    )

    (saldo_realizado, vencido_em_aberto, saldo_previsto, ultimo) = _consulta(
        """
        SELECT saldo_realizado_hoje, vencido_em_aberto, saldo_com_todo_previsto, ultimo_vencimento_previsto
        FROM leitura.conta WHERE id = %s
        """,
        [conta.id],
    )[0]
    assert saldo_realizado == Decimal("1000.00") + Decimal("500.00") - Decimal("120.00")
    assert vencido_em_aberto == Decimal("-30.00")
    assert saldo_previsto == Decimal("1000.00") + Decimal("500.00") - Decimal("120.00") - Decimal("30.00") + Decimal("200.00")
    assert ultimo == hoje + timedelta(days=15)


def test_conta_sem_lancamento_aparece_com_o_saldo_inicial(conta):
    (saldo_realizado, vencido_em_aberto, saldo_previsto, ultimo) = _consulta(
        """
        SELECT saldo_realizado_hoje, vencido_em_aberto, saldo_com_todo_previsto, ultimo_vencimento_previsto
        FROM leitura.conta WHERE id = %s
        """,
        [conta.id],
    )[0]
    assert (saldo_realizado, vencido_em_aberto, saldo_previsto, ultimo) == (
        Decimal("1000.00"), Decimal("0.00"), Decimal("1000.00"), None,
    )


@pytest.mark.parametrize(
    "alteracao",
    [
        "ALTER TABLE cash_flow_entry DROP COLUMN due_date",
        "ALTER TABLE cash_flow_entry DROP COLUMN status",
        "ALTER TABLE financial_account DROP COLUMN initial_balance",
        "ALTER TABLE cash_flow_entry ALTER COLUMN entry_amount TYPE text",
    ],
)
def test_postgres_recusa_mexer_em_coluna_que_uma_view_le(alteracao):
    """Quem alterar uma coluna lida precisa antes cuidar da view. Esse é o ponto."""
    with pytest.raises(DatabaseError, match="depend"), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(alteracao)
