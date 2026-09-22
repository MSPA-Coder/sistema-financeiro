"""Permissões funcionais lidas uma vez por objeto de usuário.

O menu e as telas perguntam `has_perm` dezenas de vezes por requisição. Antes
do cache, cada pergunta de um usuário comum era uma consulta -- dezesseis só
para abrir o detalhe de uma conta.
"""
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.services import save_function_permissions
from core.domain.identity import USER_TYPE_USER

pytestmark = pytest.mark.django_db


@pytest.fixture
def usuario():
    user = get_user_model().objects.create_user(
        username="comum", password="senha-segura", user_type=USER_TYPE_USER
    )
    save_function_permissions(user, {"transactions.view"})
    return get_user_model().objects.get(pk=user.pk)


def test_perguntas_repetidas_nao_voltam_ao_banco(usuario):
    # A primeira pergunta paga a leitura -- a deste backend e as do
    # `ModelBackend` do Django, que tem cache próprio. As seguintes, nada.
    assert usuario.has_perm("transactions.view")

    with CaptureQueriesContext(connection) as queries:
        respostas = [usuario.has_perm("transactions.view") for _ in range(10)]
        respostas.append(usuario.has_perm("transactions.delete"))

    assert respostas == [True] * 10 + [False]
    assert len(queries) == 0


def test_gravar_permissoes_descarta_o_cache(usuario):
    assert not usuario.has_perm("transactions.delete")

    save_function_permissions(usuario, {"transactions.view", "transactions.delete"})

    assert usuario.has_perm("transactions.delete")


def test_permissao_implicita_continua_valendo(usuario):
    # `transactions.update` implica `transactions.view` (PERMISSION_DEPENDENCIES);
    # o cache guarda a lista já expandida, não só as chaves concedidas.
    save_function_permissions(usuario, {"transactions.update"})

    assert usuario.has_perm("transactions.update")
    assert usuario.has_perm("transactions.view")
