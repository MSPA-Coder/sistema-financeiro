"""A autorizacao fica no servidor, por permissao nomeada.

Esconder o item do menu e apresentacao. O que impede um usuario sem a permissao
e o `permission_required` na view, e e isso que este arquivo mede.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.test import RequestFactory

from core import permissions
from core import views as core_views
from core.context_processors import _is_allowed


class _MenuItemFalso:
    def __init__(self, requires_staff=False, required_permission=None):
        self.requires_staff = requires_staff
        self.required_permission = required_permission


class _UsuarioFalso:
    def __init__(self, is_staff=False, perms=()):
        self.is_staff = is_staff
        self._perms = set(perms)

    def has_perm(self, perm):
        return perm in self._perms


def test_item_de_staff_some_para_quem_nao_e_staff():
    item = _MenuItemFalso(requires_staff=True)
    assert _is_allowed(item, _UsuarioFalso(is_staff=True)) is True
    assert _is_allowed(item, _UsuarioFalso(is_staff=False)) is False


def test_item_com_permissao_exige_a_permissao():
    item = _MenuItemFalso(required_permission="transactions.view")
    assert _is_allowed(item, _UsuarioFalso(perms=["transactions.view"])) is True
    assert _is_allowed(item, _UsuarioFalso(perms=[])) is False


def test_usuario_ausente_nao_alcanca_nada():
    # Um `user` None nao pode virar acesso liberado por acidente.
    assert _is_allowed(_MenuItemFalso(requires_staff=True), None) is False
    assert _is_allowed(_MenuItemFalso(required_permission="x"), None) is False


def test_item_sem_exigencia_e_visivel():
    assert _is_allowed(_MenuItemFalso(), _UsuarioFalso()) is True


def test_permission_required_nega_quem_nao_tem():
    fonte = inspect.getsource(permissions.permission_required)
    # A decisao precisa continuar consultando a permissao e desviar quem nao a
    # tem; se virar apenas aviso na tela, deixa de ser controle.
    assert "has_perm" in fonte or "tem_permissao" in fonte
    assert "redirect" in fonte or "PermissionDenied" in fonte or "403" in fonte


# --- Conceder privilegio e ato administrativo -------------------------------
#
# `permissions.manage` abre a tela de Permissoes. Ate esta mudanca ela tambem
# GRAVAVA: quem a tivesse sem ser administrador se selecionava em
# `?user_id=<o proprio id>`, marcava todas as caixas e todos os titulares, e
# passava a enxergar e alterar o dado financeiro inteiro.
#
# A trava equivalente para o TIPO de usuario ja existia
# (`accounts.services.user_mutation_block_message`: "somente administrator pode
# criar ou promover usuarios privilegiados"). Faltava a do ESCOPO DE DADOS.


def _requisicao_de_permissoes(user_type: str, acao: str):
    request = RequestFactory().post("/permissions/", data={"action": acao, "user_id": "1"})
    request.user = SimpleNamespace(
        id=9,
        user_type=user_type,
        is_authenticated=True,
        has_perm=lambda perm: True,  # tem `permissions.manage`; falta o tipo
    )
    return request


def _tela_com_alvo(alvo_id: int = 1):
    users = Mock()
    users.filter.return_value.first.return_value = SimpleNamespace(id=alvo_id)
    return users


@pytest.mark.parametrize(
    "acao", ["save_function_permissions", "save_owner_access", "apply_profile"]
)
def test_conceder_privilegio_exige_administrator(acao):
    """As tres acoes que concedem privilegio recusam quem nao e administrador."""
    with (
        patch("core.views.list_manageable_users", return_value=_tela_com_alvo()),
        patch("core.views.messages"),
        patch("core.views.save_function_permissions") as gravar_permissoes,
        patch("core.views.save_owner_access_matrix") as gravar_titulares,
    ):
        resposta = inspect.unwrap(core_views.permissions_view)(
            _requisicao_de_permissoes("user", acao)
        )

    assert resposta.status_code == 302
    gravar_permissoes.assert_not_called()
    gravar_titulares.assert_not_called()


def test_administrator_continua_concedendo():
    """A trava fecha o escalonamento sem tirar a tela de quem administra."""
    with (
        patch("core.views.list_manageable_users", return_value=_tela_com_alvo()),
        patch("core.views.messages"),
        patch("core.views.allowed_permission_keys", return_value=set()),
        patch("core.views.log_audit_event"),
        patch("core.views.save_function_permissions") as gravar_permissoes,
    ):
        resposta = inspect.unwrap(core_views.permissions_view)(
            _requisicao_de_permissoes("administrator", "save_function_permissions")
        )

    assert resposta.status_code == 302
    gravar_permissoes.assert_called_once()


def test_tela_esconde_os_botoes_de_gravacao_de_quem_nao_e_administrator():
    """Esconder e apresentacao -- mas a tela nao deve oferecer o que o servidor nega."""
    contexto = {}

    def _capturar(request, template, context):
        contexto.update(context)
        return SimpleNamespace(status_code=200)

    request = _requisicao_de_permissoes("user", "")
    with (
        patch("core.views.render", side_effect=_capturar),
        patch("core.views.AccountOwner"),
        patch("core.views.allowed_permission_keys", return_value=set()),
        patch("core.views.owner_access_map", return_value={}),
        patch("core.views.permission_catalog_grouped", return_value={}),
        patch("core.views.permission_catalog_sections", return_value=[]),
        patch("core.views.permission_summary", return_value={}),
    ):
        core_views._render_permissions(request, _tela_com_alvo(), SimpleNamespace(id=1), False)

    assert contexto["pode_conceder"] is False
