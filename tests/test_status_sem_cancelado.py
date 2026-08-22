"""O dominio e os filtros oferecem exatamente os status validos.

As listas de filtro nao podem ser definidas por fatia:

    STATUS_FILTER_OPTIONS = STATUS_OPTIONS[:-1]
    VIEW_FILTER_MODE_OPTIONS = VIEW_MODE_OPTIONS[:-1]

Uma mudanca na ordem retiraria silenciosamente uma opcao valida. Por isso os
testes afirmam o conteudo, e nao apenas o comprimento das listas.
"""

from __future__ import annotations

from core.domain import finance


def test_cancelado_saiu_de_todas_as_listas():
    assert not hasattr(finance, "STATUS_CANCELED")
    assert not hasattr(finance, "VIEW_CANCELED")
    assert "cancelado" not in finance.VALID_STATUSES
    assert "cancelado" not in {valor for valor, _ in finance.STATUS_OPTIONS}
    assert "cancelado" not in finance.VALID_VIEW_MODES


def test_realizado_continua_nos_filtros():
    # A regressao que a fatia teria causado, dita pelo nome.
    assert finance.STATUS_REALIZED in {v for v, _ in finance.STATUS_FILTER_OPTIONS}
    assert finance.VIEW_REALIZED in {v for v, _ in finance.VIEW_FILTER_MODE_OPTIONS}


def test_filtros_oferecem_exatamente_os_status_validos():
    assert {v for v, _ in finance.STATUS_FILTER_OPTIONS} == set(finance.VALID_STATUSES)


def test_filtro_de_modo_oferece_todos_os_modos_validos():
    assert {v for v, _ in finance.VIEW_FILTER_MODE_OPTIONS} == finance.VALID_VIEW_MODES


def test_modo_desconhecido_cai_no_padrao():
    # `cancelado` vinha de uma URL guardada nos favoritos continua sendo um
    # valor possivel de chegar; precisa degradar, nao estourar.
    assert finance.normalize_view_mode("cancelado") == finance.VIEW_PROJECTED
    assert finance.normalize_view_mode(None) == finance.VIEW_PROJECTED
    assert finance.normalize_view_mode(finance.VIEW_REALIZED) == finance.VIEW_REALIZED


def test_cancelar_lancamento_nao_existe_mais():
    from transactions import services

    assert not hasattr(services, "cancel_transaction")


def test_permissoes_orfas_sairam_do_catalogo():
    from accounts.services import PERMISSION_DEFINITIONS, PERMISSION_DEPENDENCIES

    for nome in (
        "transactions.cancel",
        "transactions.close_month",
        "transactions.reopen_month",
    ):
        assert nome not in PERMISSION_DEFINITIONS
        assert nome not in PERMISSION_DEPENDENCIES


def test_permissao_viva_de_fechamento_continua_no_catalogo():
    # Controle positivo: fechar e reabrir mes continuam existindo, por
    # `settings.monthly_close.manage`. Sem esta metade, o teste acima passaria
    # tambem se a capacidade tivesse sido perdida junto.
    from accounts.services import PERMISSION_DEFINITIONS

    assert "settings.monthly_close.manage" in PERMISSION_DEFINITIONS


def test_perfis_nao_concedem_permissao_inexistente():
    # Uma permissao concedida a um perfil e ausente do catalogo e um erro que
    # so aparece quando alguem abre a tela de permissoes.
    from accounts.services import PERMISSION_DEFINITIONS, PROFILE_DEFINITIONS

    for nome, perfil in PROFILE_DEFINITIONS.items():
        desconhecidas = set(perfil["permissions"]) - set(PERMISSION_DEFINITIONS)
        assert not desconhecidas, f"perfil {nome} concede {desconhecidas}"


def test_menu_de_permissoes_so_lista_permissao_existente():
    from accounts.services import PERMISSION_DEFINITIONS, PERMISSION_MENU_GROUPS

    for grupo, permissoes in PERMISSION_MENU_GROUPS:
        desconhecidas = set(permissoes) - set(PERMISSION_DEFINITIONS)
        assert not desconhecidas, f"grupo {grupo} lista {desconhecidas}"
