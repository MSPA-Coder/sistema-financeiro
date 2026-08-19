"""Escritas exigem token CSRF.

O cliente de teste do Django dispensa CSRF por padrao; `client_com_csrf` religa
a verificacao, porque desligar exatamente o controle que se quer medir tornaria
o teste decorativo.
"""

from __future__ import annotations

from django.conf import settings
from django.test import override_settings


def test_post_sem_token_e_rejeitado(client_com_csrf):
    resposta = client_com_csrf.post("/login", {"username": "x", "password": "y"})
    assert resposta.status_code == 403


def test_post_com_token_invalido_e_rejeitado(client_com_csrf):
    resposta = client_com_csrf.post(
        "/login", {"csrfmiddlewaretoken": "token-inventado", "username": "x", "password": "y"}
    )
    assert resposta.status_code == 403


def _decisao_do_csrf(origin: str | None, *, secure: bool = False):
    """Roda o middleware de CSRF sobre um POST montado a mao.

    Exercita a decisao sem passar pela view, que consultaria o banco -- a suite
    minima nao tem banco por desenho. `None` significa aceito; uma resposta
    significa recusado.
    """
    from django.http import HttpResponse
    from django.middleware.csrf import CsrfViewMiddleware, get_token
    from django.test import RequestFactory

    fabrica = RequestFactory()
    requisicao_get = fabrica.get("/login")
    token = get_token(requisicao_get)
    cookie = requisicao_get.META["CSRF_COOKIE"]

    extras = {"HTTP_ORIGIN": origin} if origin is not None else {}
    requisicao = fabrica.post(
        "/login", {"csrfmiddlewaretoken": token}, secure=secure, **extras
    )
    requisicao.COOKIES[settings.CSRF_COOKIE_NAME] = cookie

    middleware = CsrfViewMiddleware(lambda _r: HttpResponse())
    middleware.process_request(requisicao)
    return middleware.process_view(requisicao, lambda _r: HttpResponse(), (), {})


def test_post_valido_do_navegador_e_aceito():
    """O teste que faltava: um POST como o navegador realmente monta.

    A suite anterior so exercitava o caminho negativo, e por isso ficou verde
    enquanto o login estava quebrado no navegador. Um cliente de linha de
    comando nao manda `Origin` nenhum e nao reproduzia a falha.
    """
    assert _decisao_do_csrf("http://testserver") is None


@override_settings(CSRF_TRUSTED_ORIGINS=["https://bancario-mspa.duckdns.org"])
def test_post_https_de_origem_confiavel_e_aceito():
    assert _decisao_do_csrf("https://bancario-mspa.duckdns.org", secure=True) is None


def test_origin_nulo_e_recusado():
    # Documenta o mecanismo da falha: era este o `Origin` que o navegador
    # mandava sob `Referrer-Policy: no-referrer`.
    assert _decisao_do_csrf("null") is not None


def test_referrer_policy_nao_anula_o_origin():
    # Amarra a causa raiz ao controle: `no-referrer` faria o navegador mandar
    # `Origin: null` e o teste acima mostra o que acontece nesse caso.
    assert settings.SECURE_REFERRER_POLICY != "no-referrer"


def test_middleware_de_csrf_esta_ativo():
    assert "django.middleware.csrf.CsrfViewMiddleware" in settings.MIDDLEWARE


def test_middleware_de_csp_esta_ativo():
    assert any("ContentSecurityPolicyMiddleware" in m for m in settings.MIDDLEWARE)
