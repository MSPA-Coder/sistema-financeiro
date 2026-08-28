"""Contratos puros do relatório de planejamento anual.

Os testes não abrem banco: a suíte mínima deste projeto protege contratos de
regra e segurança sem depender de uma instância PostgreSQL local.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser

from reports import services


def test_planejamento_tem_ano_calendario_e_janela_movel_de_13_meses():
    calendario = services._planning_months(date(2026, 8, 17), services.ANNUAL_PLANNING_CALENDAR)
    movel = services._planning_months(date(2026, 8, 17), services.ANNUAL_PLANNING_ROLLING_13)

    assert calendario == [date(2026, month, 1) for month in range(1, 13)]
    assert len(movel) == 13
    assert movel[0] == date(2026, 2, 1)
    assert movel[6] == date(2026, 8, 1)
    assert movel[-1] == date(2027, 2, 1)


def test_classificacao_de_recorrencia_usa_is_recurring():
    recurring = {"recurring": Decimal("0.00"), "non_recurring": Decimal("0.00")}
    installment = SimpleNamespace(is_recurring=False)
    recurring_entry = SimpleNamespace(is_recurring=True)

    services._planning_add_category(recurring, installment, Decimal("10.00"))
    services._planning_add_category(recurring, recurring_entry, Decimal("4.25"))

    assert recurring == {"recurring": Decimal("4.25"), "non_recurring": Decimal("10.00")}


def test_filtro_de_ids_descarta_bool_invalidos_e_nao_duplica():
    assert services._planning_id_filter([1, "1", True, 0, -2, "x", None]) == [1]
    assert services._planning_id_filter(None) is None


def test_usuario_anonimo_falha_fechado_antes_de_consultar_contas():
    assert services._authorized_planning_accounts(None, None, None) == ([], [])


def test_apresentacao_falha_fechado_para_usuario_anonimo():
    report = services.annual_planning_presentation(AnonymousUser(), date(2026, 8, 1))

    assert report["owner_columns"] == []
    assert report["rows"] == []
