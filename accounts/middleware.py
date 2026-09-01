"""Trava de troca de senha pendente.

Quando um administrador redefine a senha de alguém, essa senha passa a ser
conhecida por duas pessoas. `must_change_password` existe para encurtar essa
janela ao primeiro acesso — e **só vale se for verificada em toda requisição**.

Até 30/08/2026 a verificação acontecia apenas em `AppLoginView.form_valid`, no
instante do login. Bastava digitar outra URL depois do desvio para continuar
navegando com a senha que o administrador conhece, e a marca ficava ligada para
sempre sem efeito. Este middleware fecha essa lacuna.

O mesmo contrato roda nos três aplicativos Flask do mantenedor por
`sharedauth.access.requer_troca_de_senha`; aqui ele é nativo, porque o Django
resolve sessão e autenticação por conta própria e a biblioteca deliberadamente
não entra nesse caminho.
"""
from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse


def _caminhos_isentos() -> tuple[str, ...]:
    """Caminhos que continuam alcançáveis com a troca pendente.

    A própria tela de troca, senão o desvio vira laço na página que existe para
    sair da situação. O logout, senão a pessoa fica presa dentro do aplicativo
    sem poder nem sair. O login, que já trata sessão por conta própria. A sonda
    de saúde, senão o contêiner passa a ser reportado como doente justamente
    para quem está com a senha vencida. E os estáticos, senão a tela de troca
    chega sem CSS.

    Resolvido por `reverse()` a cada chamada, e não numa constante de módulo:
    a resolução de URL do Django não está pronta na importação do middleware.
    """
    return (
        reverse("accounts:change_password"),
        reverse("logout"),
        reverse("login"),
        reverse("health_check"),
        "/health",
        f"/{settings.STATIC_URL.lstrip('/')}",
        f"/{settings.MEDIA_URL.lstrip('/')}",
    )


class MustChangePasswordMiddleware:
    """Prende quem está com troca de senha pendente na tela de troca."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        usuario = getattr(request, "user", None)
        if (
            usuario is not None
            and usuario.is_authenticated
            and getattr(usuario, "must_change_password", False)
            and not request.path.startswith(_caminhos_isentos())
        ):
            destino = reverse("accounts:change_password")
            # Uma troca de fragmento não pode devolver a tela de troca dentro
            # de um pedaço de página: respostas 3xx não disparam `HX-Redirect`,
            # então a navegação completa precisa vir num 200 com o cabeçalho --
            # mesmo tratamento que `HtmxAuthenticationMiddleware` dá ao redirect
            # de sessão expirada.
            if getattr(request, "htmx", False):
                resposta = HttpResponse(status=200)
                resposta["HX-Redirect"] = destino
                return resposta
            return redirect(destino)

        return self.get_response(request)
