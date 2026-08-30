"""Backends de autenticacao e de autorizacao funcional."""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.http import HttpRequest
from sharedauth.logs import sanitizar_log

logger = logging.getLogger(__name__)


class CaseInsensitiveUsernameBackend(ModelBackend):
    """Autentica aceitando o nome de usuario em qualquer caixa.

    O cadastro guarda o nome como foi digitado ("Mariano"), mas a tela de login
    nao deve exigir que o usuario reproduza a capitalizacao. A unicidade e
    garantida pela constraint `app_user_username_key`, entao a busca
    case-insensitive resolve no maximo um registro.
    """

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ):
        if username is None:
            username = kwargs.get("username")
        if username is None or password is None:
            return None

        # `.strip()` tira espaco das pontas, NAO a quebra de linha do meio
        # -- e a do meio e o que forja uma segunda linha de log, indistinguivel
        # de um evento real. Os tres `logger.warning` abaixo recebem este valor;
        # `sanitizar_log` neutraliza a quebra antes de escrever.
        username = username.strip()
        if not username:
            return None

        user_model = get_user_model()
        try:
            user = user_model._default_manager.get(username__iexact=username)
        except user_model.DoesNotExist:
            logger.warning("auth_user_not_found username=%s", sanitizar_log(username))
            return None

        if not self.user_can_authenticate(user):
            logger.warning("auth_user_inactive username=%s", sanitizar_log(username))
            return None

        if user.check_password(password):
            logger.info("auth_success user_id=%s", user.id)
            return user

        logger.warning("auth_invalid_password username=%s", sanitizar_log(username))
        return None


class AppPermissionBackend(ModelBackend):
    """Resolve permissoes funcionais via o catalogo AppPermission/UserPermission.

    O Django cria permissoes automaticamente por modelo (`app_label.add_x`
    etc.), mas as chaves usadas por `@permission_required` neste projeto
    (ex.: "transactions.create", "tables.owners.manage") nao correspondem a
    esse esquema -- sao chaves funcionais com multiplos pontos. Este backend as
    trata como chaves opacas e resolve contra UserPermission, delegando a
    logica (incluindo o bypass para administradores) a
    accounts.services.has_function_permission.

    Apenas contribui com autorizacao: authenticate() sempre retorna None,
    deixando a autenticacao para CaseInsensitiveUsernameBackend.
    """

    def authenticate(
        self, request: HttpRequest | None, username: str | None = None, password: str | None = None, **kwargs: Any
    ) -> None:
        return None

    def has_perm(self, user_obj, perm, obj=None):
        from accounts.services import has_function_permission

        return has_function_permission(user_obj, perm)

    def has_module_perms(self, user_obj, app_label):
        return False

    def get_all_permissions(self, user_obj, obj=None):
        return set()
