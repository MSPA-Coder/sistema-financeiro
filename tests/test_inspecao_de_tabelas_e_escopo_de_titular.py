"""Duas telas que devolviam dado de outro titular a quem tinha só a permissão de "ver".

1. `/settings/database/` inspecionava linhas BRUTAS de qualquer tabela da lista de
   saúde (`app_user` com o hash de senha, lançamentos, extratos, auditoria) para
   quem tivesse apenas `settings.view`. Permissão de LEITURA da tela de
   Configurações não é permissão de ler o banco inteiro, e a inspeção não aplica
   escopo de titular. Agora é só do administrador, e o hash de senha nunca sai.

2. Renomear, excluir e listar titulares só conferia `tables.owners.manage`. Essa
   permissão diz que a pessoa gerencia titulares, não quais: as contas do mesmo
   titular já respeitavam o escopo (`can_access_owner`), os titulares não.

Cada teste mede o efeito visível (o que a resposta contém, o que ficou gravado), não
o texto do código. Precisam de PostgreSQL, como `test_invariantes_persistidos.py`.
"""

from __future__ import annotations

import pytest
from django.test import Client

from accounts.models import AccountOwner, AppPermission, AppUser, UserOwnerAccess, UserPermission
from accounts.services import delete_owner, list_owners, update_owner
from core.domain.identity import USER_TYPE_ADMINISTRATOR, USER_TYPE_USER
from core.services import inspect_table

pytestmark = pytest.mark.django_db


def _usuario(nome: str, tipo: str, permissoes: tuple[str, ...] = (), titulares: tuple[AccountOwner, ...] = ()) -> AppUser:
    usuario = AppUser.objects.create(username=nome, user_type=tipo, is_active=True)
    usuario.set_password("Senha-so-de-teste-1!")
    usuario.save()
    for chave in permissoes:
        UserPermission.objects.create(user=usuario, permission=AppPermission.objects.get(name=chave), allowed=True)
    for titular in titulares:
        UserOwnerAccess.objects.create(
            user=usuario, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
        )
    return usuario


def _logado(usuario: AppUser) -> Client:
    cliente = Client()
    cliente.force_login(usuario)
    return cliente


@pytest.fixture
def dois_titulares() -> tuple[AccountOwner, AccountOwner]:
    return AccountOwner.objects.create(name="Titular Ana"), AccountOwner.objects.create(name="Titular Bia")


# --- Inspeção de tabelas -------------------------------------------------------


def test_quem_so_ve_configuracoes_nao_inspeciona_tabela_nenhuma(dois_titulares):
    _usuario("admin_da_casa", USER_TYPE_ADMINISTRATOR)
    leitor = _usuario("leitor", USER_TYPE_USER, permissoes=("settings.view",), titulares=dois_titulares[:1])

    for tabela in ("app_user", "account_owner", "cash_flow_entry"):
        resposta = _logado(leitor).get("/settings/database/", {"table_name": tabela})
        corpo = resposta.content.decode("utf-8")

        assert resposta.status_code == 200
        # A view nem chega a ler a tabela: o template esconde o cartão, mas uma
        # segunda barreira não desculpa carregar linhas de todos os titulares.
        assert not list(resposta.context["table_rows"]), f"linhas carregadas para quem não pode ({tabela})"
        assert "pbkdf2_" not in corpo, f"hash de senha na página ({tabela})"
        assert "admin_da_casa" not in corpo, f"linha de outro usuário na página ({tabela})"
        assert "Titular Bia" not in corpo, f"titular sem acesso na página ({tabela})"
        assert "Inspeção de Dados" not in corpo


def test_administrador_inspeciona_mas_o_hash_de_senha_fica_oculto(dois_titulares):
    admin = _usuario("admin_da_casa", USER_TYPE_ADMINISTRATOR)

    resposta = _logado(admin).get("/settings/database/", {"table_name": "app_user"})
    corpo = resposta.content.decode("utf-8")

    assert resposta.status_code == 200
    assert "Inspeção de Dados" in corpo
    assert "admin_da_casa" in corpo, "o administrador deve conseguir inspecionar"
    assert "pbkdf2_" not in corpo
    assert "•••" in corpo


def test_inspect_table_oculta_a_coluna_de_senha_em_qualquer_chamador():
    _usuario("alguem", USER_TYPE_USER)

    colunas, linhas = inspect_table("app_user")

    posicao = colunas.index("password")
    assert linhas, "a tabela tem o usuário criado acima"
    assert {linha[posicao] for linha in linhas} == {"•••"}
    assert not any("pbkdf2_" in str(valor) for linha in linhas for valor in linha)


def test_inspect_table_continua_recusando_tabela_fora_da_lista():
    assert inspect_table("django_session") == ([], [])
    assert inspect_table("app_user; drop table app_user") == ([], [])


# --- Escopo de titular ---------------------------------------------------------

GERENTE = ("tables.view", "tables.owners.manage")


def test_gerente_restrito_nao_renomeia_titular_alheio(dois_titulares):
    minha, alheia = dois_titulares
    gerente = _usuario("gerente", USER_TYPE_USER, permissoes=GERENTE, titulares=(minha,))

    _logado(gerente).post(f"/tables/owners/{alheia.id}/update/", {"name": "RENOMEADA"})

    alheia.refresh_from_db()
    assert alheia.name == "Titular Bia"


def test_gerente_restrito_renomeia_o_proprio_titular(dois_titulares):
    minha, _alheia = dois_titulares
    gerente = _usuario("gerente", USER_TYPE_USER, permissoes=GERENTE, titulares=(minha,))

    _logado(gerente).post(f"/tables/owners/{minha.id}/update/", {"name": "Ana Maria"})

    minha.refresh_from_db()
    assert minha.name == "Ana Maria"


def test_gerente_restrito_nao_exclui_titular_alheio(dois_titulares):
    minha, alheia = dois_titulares
    gerente = _usuario("gerente", USER_TYPE_USER, permissoes=GERENTE, titulares=(minha,))

    _logado(gerente).post(f"/tables/owners/{alheia.id}/delete/")

    assert AccountOwner.objects.filter(id=alheia.id).exists()


def test_gerente_restrito_exclui_o_proprio_titular_sem_contas(dois_titulares):
    minha, _alheia = dois_titulares
    gerente = _usuario("gerente", USER_TYPE_USER, permissoes=GERENTE, titulares=(minha,))

    _logado(gerente).post(f"/tables/owners/{minha.id}/delete/")

    assert not AccountOwner.objects.filter(id=minha.id).exists()


def test_listagem_de_titulares_mostra_so_os_do_escopo(dois_titulares):
    minha, _alheia = dois_titulares
    gerente = _usuario("gerente", USER_TYPE_USER, permissoes=GERENTE, titulares=(minha,))

    corpo = _logado(gerente).get("/tables/owners/").content.decode("utf-8")

    assert "Titular Ana" in corpo
    assert "Titular Bia" not in corpo


def test_administrador_ve_e_gerencia_todos_os_titulares(dois_titulares):
    _minha, alheia = dois_titulares
    admin = _usuario("admin", USER_TYPE_ADMINISTRATOR)

    assert set(list_owners(admin)) == set(AccountOwner.objects.all())
    update_owner(admin, alheia, "Bia Souza")
    alheia.refresh_from_db()
    assert alheia.name == "Bia Souza"
    delete_owner(admin, alheia)
    assert not AccountOwner.objects.filter(id=alheia.id).exists()


def test_servico_recusa_titular_alheio_com_mensagem_clara(dois_titulares):
    minha, alheia = dois_titulares
    gerente = _usuario("gerente", USER_TYPE_USER, permissoes=GERENTE, titulares=(minha,))

    with pytest.raises(ValueError, match="Acesso negado"):
        update_owner(gerente, alheia, "Qualquer")
    with pytest.raises(ValueError, match="Acesso negado"):
        delete_owner(gerente, alheia)
