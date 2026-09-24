"""Senha redefinida por administrador vale ate o primeiro acesso.

Ate 30/08/2026 a obrigacao de trocar era aplicada **apenas no instante do
login** (`AppLoginView.form_valid`). Bastava digitar outra URL depois do desvio
para continuar navegando com a senha que o administrador conhece, e a marca
ficava ligada para sempre sem efeito. Este arquivo mede a trava que fechou essa
lacuna, e que roda em toda requisicao.

O middleware e exercitado com `RequestFactory` e um duplo de usuario, que e
tudo o que ele consulta. A redefinicao e a troca gravam a conta, e por isso
rodam na camada com banco (`django_db`).
"""

from __future__ import annotations

import pytest
from django.test import RequestFactory

from accounts.middleware import MustChangePasswordMiddleware

DESTINO = "/change-password/"


class _Usuario:
    """Substitui `request.user`: o middleware so olha estas duas coisas."""

    def __init__(self, *, autenticado: bool = True, trocar: bool = False) -> None:
        self.is_authenticated = autenticado
        self.must_change_password = trocar


def _middleware(resposta_da_view=None):
    marcador = resposta_da_view if resposta_da_view is not None else "chegou na view"
    return MustChangePasswordMiddleware(lambda _request: marcador)


def _requisicao(caminho: str, *, usuario, htmx: bool = False):
    requisicao = RequestFactory().get(caminho)
    requisicao.user = usuario
    requisicao.htmx = htmx
    return requisicao


def test_marca_ligada_desvia_qualquer_rota_para_a_troca():
    resposta = _middleware()(
        _requisicao("/transactions/", usuario=_Usuario(trocar=True))
    )

    assert resposta.status_code == 302
    assert resposta["Location"] == DESTINO


def test_marca_desligada_nao_atrapalha():
    resposta = _middleware()(
        _requisicao("/transactions/", usuario=_Usuario(trocar=False))
    )

    assert resposta == "chegou na view"


def test_anonimo_nao_e_desviado():
    # Quem nao entrou nao tem senha a trocar; o assunto dele e o login, e quem
    # responde por isso e o `login_required` de cada view.
    resposta = _middleware()(
        _requisicao("/transactions/", usuario=_Usuario(autenticado=False, trocar=True))
    )

    assert resposta == "chegou na view"


@pytest.mark.parametrize(
    "caminho",
    [
        DESTINO,
        "/logout",
        "/login",
        "/health/",
        "/health",
        "/static/css/core/application.css",
        "/media/qualquer.pdf",
    ],
)
def test_caminhos_isentos_continuam_alcancaveis(caminho):
    # A propria tela de troca, senao o desvio vira laco na pagina que existe
    # para sair da situacao. O logout, senao a pessoa fica presa dentro do
    # aplicativo. A sonda, senao o contêiner e reportado como doente
    # justamente para quem esta com a senha vencida. Os estaticos, senao a tela
    # de troca chega sem CSS.
    resposta = _middleware()(_requisicao(caminho, usuario=_Usuario(trocar=True)))

    assert resposta == "chegou na view", f"{caminho} foi desviado para a troca"


def test_requisicao_htmx_recebe_hx_redirect():
    # Respostas 3xx nao disparam `HX-Redirect`: sem isto o HTMX trocaria o
    # fragmento pela tela de troca dentro de um pedaco de pagina. Mesmo
    # tratamento que `HtmxAuthenticationMiddleware` da a sessao expirada.
    resposta = _middleware()(
        _requisicao("/transactions/", usuario=_Usuario(trocar=True), htmx=True)
    )

    assert resposta.status_code == 200
    assert resposta["HX-Redirect"] == DESTINO


def test_a_marca_e_consultada_a_cada_requisicao():
    # Depois da troca, a mesma aplicacao tem de liberar sem reiniciar.
    usuario = _Usuario(trocar=True)
    middleware = _middleware()

    assert middleware(_requisicao("/transactions/", usuario=usuario)).status_code == 302
    usuario.must_change_password = False
    assert middleware(_requisicao("/transactions/", usuario=usuario)) == "chegou na view"


def test_o_middleware_esta_instalado_e_depois_do_que_ele_depende():
    # Precisa de `request.user` (AuthenticationMiddleware) e de `request.htmx`
    # (HtmxMiddleware). Instalado antes de qualquer um dos dois, a trava nao
    # veria nem o usuario nem o HTMX -- e falharia aberta, em silencio.
    from django.conf import settings

    instalados = list(settings.MIDDLEWARE)
    alvo = "accounts.middleware.MustChangePasswordMiddleware"

    assert alvo in instalados
    assert instalados.index("django.contrib.auth.middleware.AuthenticationMiddleware") < instalados.index(alvo)
    assert instalados.index("django_htmx.middleware.HtmxMiddleware") < instalados.index(alvo)


# --- redefinicao pelo administrador --------------------------------------


def _conta(username="redefinida", senha="Senha-Atual-Longa-1"):
    from accounts.models import AppUser

    return AppUser.objects.create_user(username=username, password=senha)


@pytest.mark.django_db
def test_a_redefinicao_sorteia_senha_ditavel_e_liga_a_marca():
    # Alfabeto sem `0/O` e `1/l/I` -- a senha vai ser ditada --, o mesmo dos
    # tres apps Flask (`sharedauth.passwords`). E a conta sai obrigada a trocar.
    from accounts import services

    usuario = _conta()
    senhas = {services.reset_managed_user_password(usuario) for _ in range(20)}

    usuario.refresh_from_db()
    assert usuario.must_change_password is True
    assert all(not set(senha) & set("0O1lI") for senha in senhas)
    assert len(senhas) == 20


@pytest.mark.django_db
def test_o_sorteio_respeita_o_tamanho_minimo_configurado(monkeypatch):
    # A politica deste app e configuravel e pode passar do padrao de 12 da
    # biblioteca -- o banco local esta em 15. Sem isto, a redefinicao recusaria
    # a propria senha que acabou de sortear.
    from accounts import services

    monkeypatch.setattr(services, "current_min_length", lambda: 20)

    senha = services.reset_managed_user_password(_conta())

    assert len(senha) >= 20


def test_o_tamanho_minimo_tem_fallback_sem_banco():
    # Lido em toda redefinicao; um banco fora do ar nao pode transformar isso
    # em erro 500 no meio da tela de permissoes.
    from accounts.password_validators import DEFAULT_MIN_LENGTH, current_min_length

    assert current_min_length() >= DEFAULT_MIN_LENGTH


@pytest.mark.django_db
def test_redigitar_a_senha_temporaria_nao_conclui_a_troca():
    # O caso que esvaziaria a obrigacao: a marca se apagaria e a senha que o
    # administrador conhece continuaria valendo. Mesma regra que
    # `sharedauth.passwords.validar_troca` aplica nos tres apps Flask.
    from accounts import services

    usuario = _conta()
    temporaria = services.reset_managed_user_password(usuario)

    with pytest.raises(ValueError, match="diferente"):
        services.change_user_password(usuario, temporaria, temporaria, temporaria)

    usuario.refresh_from_db()
    assert usuario.must_change_password is True
    assert usuario.check_password(temporaria)


@pytest.mark.django_db
def test_a_senha_temporaria_nao_entra_na_auditoria():
    # Nao ha pergunta que ela responda e ha muitas que ela abre. O snapshot de
    # auditoria do usuario nunca inclui senha nem hash.
    from accounts import services
    from core import views

    usuario = _conta()
    temporaria = services.reset_managed_user_password(usuario)
    usuario.refresh_from_db()

    snapshot = views._user_audit_snapshot(usuario)

    assert temporaria not in repr(snapshot)
    assert usuario.password not in repr(snapshot)
    assert not {chave for chave in snapshot if "password" in chave} - {"must_change_password"}
