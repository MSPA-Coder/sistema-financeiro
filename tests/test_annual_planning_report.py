"""Contratos puros do relatório de planejamento anual.

Os testes não abrem banco: a suíte mínima deste projeto protege contratos de
regra e segurança sem depender de uma instância PostgreSQL local.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from reports import services, views


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


def test_ausencia_do_parametro_vale_todos_e_lista_vazia_nao_e_ausencia():
    """Carga sem query string precisa marcar todos os titulares e contas.

    `QueryDict.getlist` devolve `[]` tanto para "parametro ausente" quanto para
    "parametro presente e vazio", e `_planning_id_filter([])` devolve `[]` --
    que o filtro le como "nenhum escolhido". Ler `getlist` direto fazia a
    primeira carga da tela filtrar por lista vazia: nenhuma opcao vinha
    marcada e o relatorio saia sem uma linha sequer.
    """
    fabrica = RequestFactory()

    sem_query = fabrica.get("/reports/annual-planning/")
    assert views._multi_id_param(sem_query, "owner_ids") is None
    assert views._multi_id_param(sem_query, "account_ids") is None

    com_ids = fabrica.get("/reports/annual-planning/?owner_ids=3&owner_ids=7")
    assert views._multi_id_param(com_ids, "owner_ids") == [3, 7]

    # Presente porem invalido continua fechando: nao vira "todos".
    invalido = fabrica.get("/reports/annual-planning/?owner_ids=abc")
    assert views._multi_id_param(invalido, "owner_ids") == []


def test_none_seleciona_todas_as_contas_permitidas_e_lista_vazia_nenhuma():
    """O contrato que a view consome: `None` e "todos", `[]` e "nenhum"."""
    usuario = SimpleNamespace(is_authenticated=True)

    with mock.patch.object(services, "accessible_owner_ids", return_value=[4, 9]):
        contas, ids = services._authorized_planning_accounts(usuario, [], None)

    assert (contas, ids) == ([], [])


def test_usuario_anonimo_falha_fechado_antes_de_consultar_contas():
    assert services._authorized_planning_accounts(None, None, None) == ([], [])


def test_apresentacao_falha_fechado_para_usuario_anonimo():
    report = services.annual_planning_presentation(AnonymousUser(), date(2026, 8, 1))

    assert report["owner_columns"] == []
    assert report["rows"] == []
