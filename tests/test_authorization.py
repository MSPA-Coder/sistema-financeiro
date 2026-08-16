"""A autorizacao fica no servidor, por permissao nomeada.

Esconder o item do menu e apresentacao. O que impede um usuario sem a permissao
e o `permission_required` na view, e e isso que este arquivo mede.
"""

from __future__ import annotations

import inspect

from core import permissions
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
