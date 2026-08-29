"""Guardas de autorizacao por rota e de coerencia do catalogo.

Duas coisas que a suite nao protegia, e ambas custaram caro:

1. Uma view podia nascer sem `@permission_required` e nada apontava. Quatro
   telas -- Dashboard, Projecoes, Posicao por conta e Proximos movimentos --
   ficaram assim por tempo indeterminado: a chave existia no catalogo, era
   distribuida nos perfis e aparecia na tela de Permissoes, e nenhuma linha de
   codigo a verificava. Desmarcar a caixa nao restringia nada.

2. Uma chave podia viver no catalogo sem quem a consumisse. O comentario de
   `PERMISSION_DEFINITIONS` afirma que so entram chaves que "algum
   @permission_required ou item de menu realmente referencia hoje" -- era
   falso, e nada verificava a afirmacao.

Nao abre banco: le a URLconf e a arvore de menu, que sao estrutura declarada,
nao dado. E a mesma razao pela qual estes testes conseguem existir nesta suite.
"""

from __future__ import annotations

from django.urls import get_resolver

from accounts.services import PERMISSION_DEFINITIONS
from core.context_processors import _build_menu_items

#: Rotas que respondem sem exigir permissao funcional, cada uma com o motivo.
#: Entrar aqui e uma decisao consciente; e o unico jeito de uma rota nova
#: passar sem `@permission_required`.
ROTAS_ABERTAS: dict[str, str] = {
    "": "redireciona para o login; nao renderiza nada",
    "login": "autenticacao",
    "login/": "autenticacao",
    "logout": "encerrar sessao nao pode depender de permissao",
    "logout/": "encerrar sessao nao pode depender de permissao",
    "health": "sonda de infraestrutura, sem sessao",
    "health/": "sonda de infraestrutura, sem sessao",
    "change-password/": "todo usuario troca a propria senha, sempre",
    "reports/upcoming-movements/": (
        "tela de pouso: e o LOGIN_REDIRECT_URL e o destino de negacao dos "
        "demais relatorios. Exigir permissao aqui deixaria o usuario negado "
        "sem lugar para cair."
    ),
}

#: Chaves verificadas dentro do corpo de uma view, nao por decorator. Sao
#: legitimas -- so nao aparecem na varredura da URLconf.
VERIFICADAS_NO_CORPO: dict[str, str] = {
    "tables.users.manage": "core/views.py, em settings_home_view",
}

#: Chave que sobrevive no catalogo sem ninguem que a verifique. As outras
#: quatro que estavam aqui foram removidas na `accounts/migrations/0007`; esta
#: fica porque a ausencia de verificacao e DELIBERADA, nao esquecimento -- ver
#: o motivo abaixo. Uma orfa nova, por esquecimento, reprova a suite.
ORFAS_CONHECIDAS: dict[str, str] = {
    "reports.upcoming_movements.view": (
        "a rota e aberta de proposito (tela de pouso), e o item de menu nao e "
        "restrito -- restringi-lo esconderia o link de quem cai ali no login"
    ),
}


def _rotas():
    """(padrao, view) de toda rota do projeto, menos o admin do Django."""

    def caminhar(resolver, prefixo=""):
        for entrada in resolver.url_patterns:
            padrao = prefixo + str(entrada.pattern)
            if hasattr(entrada, "url_patterns"):
                yield from caminhar(entrada, padrao)
            else:
                yield padrao, entrada.callback

    for padrao, view in caminhar(get_resolver()):
        # O admin e do Django e tem o proprio controle de acesso.
        if not padrao.startswith("admin/"):
            yield padrao, view


def _permissoes_do_menu(itens=None) -> set[str]:
    resultado: set[str] = set()
    for item in _build_menu_items() if itens is None else itens:
        if item.required_permission:
            resultado.add(item.required_permission)
        resultado |= _permissoes_do_menu(item.children)
    return resultado


def test_toda_rota_exige_permissao_ou_esta_declarada_aberta() -> None:
    """Uma view nova sem decorator reprova aqui, em vez de silenciosamente abrir."""
    sem_guarda = sorted(
        padrao
        for padrao, view in _rotas()
        if not getattr(view, "permissao_exigida", None) and padrao not in ROTAS_ABERTAS
    )

    assert not sem_guarda, (
        "Rotas sem @permission_required e sem justificativa: "
        f"{sem_guarda}. Aplique o decorator ou declare a rota em "
        "ROTAS_ABERTAS, com o motivo escrito."
    )


def test_lista_de_rotas_abertas_nao_apodrece() -> None:
    """Rota que ganhou permissao precisa sair da lista, ou a lista vira ficcao."""
    padroes = {padrao for padrao, _ in _rotas()}

    inexistentes = sorted(set(ROTAS_ABERTAS) - padroes)
    assert not inexistentes, f"ROTAS_ABERTAS cita rotas que nao existem mais: {inexistentes}"

    ja_protegidas = sorted(
        padrao
        for padrao, view in _rotas()
        if padrao in ROTAS_ABERTAS and getattr(view, "permissao_exigida", None)
    )
    assert not ja_protegidas, (
        f"Estas rotas exigem permissao e continuam listadas como abertas: {ja_protegidas}"
    )


def test_nenhuma_chave_do_catalogo_fica_sem_quem_a_verifique() -> None:
    """O catalogo nao pode prometer um controle que nao existe.

    Uma chave orfa e pior que chave ausente: ela aparece na tela de Permissoes,
    o administrador a desmarca acreditando ter restringido algo, e nada muda.
    """
    por_rota = {
        permissao
        for _, view in _rotas()
        if (permissao := getattr(view, "permissao_exigida", None))
    }
    consumidas = por_rota | _permissoes_do_menu() | set(VERIFICADAS_NO_CORPO)

    orfas = set(PERMISSION_DEFINITIONS) - consumidas - set(ORFAS_CONHECIDAS)
    assert not orfas, (
        f"Chaves no catalogo que ninguem verifica: {sorted(orfas)}. "
        "Ligue a verificacao, remova a chave, ou registre em ORFAS_CONHECIDAS."
    )


def test_orfas_conhecidas_continuam_orfas() -> None:
    """Quando uma orfa ganhar consumidor, ela sai da lista -- e o teste cobra."""
    por_rota = {
        permissao
        for _, view in _rotas()
        if (permissao := getattr(view, "permissao_exigida", None))
    }
    consumidas = por_rota | _permissoes_do_menu() | set(VERIFICADAS_NO_CORPO)

    resolvidas = sorted(set(ORFAS_CONHECIDAS) & consumidas)
    assert not resolvidas, (
        f"Estas chaves deixaram de ser orfas: {resolvidas}. Remova-as de ORFAS_CONHECIDAS."
    )
