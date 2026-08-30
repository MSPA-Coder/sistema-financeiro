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
   falso para nove chaves, e nada verificava a afirmacao.

Nao ha lista de excecao para a segunda guarda, e isso e deliberado. Uma chave
orfa chegou a ficar registrada aqui como "conhecida", o que deixava a suite
verde sobre um defeito da aplicacao. Teste existe para validar a aplicacao;
quando ele precisa de uma excecao para passar, quem tem de mudar e quase
sempre a aplicacao. Naquele caso era: a tela ficava sem permissao so por ser o
destino fixo do login, e a correcao foi tornar esse destino derivado.

Nao abre banco: le a URLconf e a arvore de menu, que sao estrutura declarada,
nao dado. E a mesma razao pela qual estes testes conseguem existir nesta suite.
"""

from __future__ import annotations

from django.urls import get_resolver

from accounts.services import PERMISSION_DEFINITIONS
from core.context_processors import _build_menu_items, primeira_tela_permitida

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
    "inicio/": (
        "nao e tela: resolve para onde a pessoa pode ir e redireciona. Exigir "
        "permissao no destino do login e da negacao seria o laco que ela "
        "existe para evitar; quem nao pode abrir nada recebe 403 com aviso."
    ),
}

#: Chaves verificadas dentro do corpo de uma view, nao por decorator. Sao
#: legitimas -- so nao aparecem na varredura da URLconf.
VERIFICADAS_NO_CORPO: dict[str, str] = {
    "tables.users.manage": "core/views.py, em settings_home_view",
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

    orfas = set(PERMISSION_DEFINITIONS) - consumidas
    assert not orfas, (
        f"Chaves no catalogo que ninguem verifica: {sorted(orfas)}. "
        "Ligue a verificacao ou remova a chave -- nao ha lista de perdao aqui, "
        "de proposito: chave que nao guarda nada nao pode aparecer na tela de "
        "Permissoes como se guardasse."
    )


class _UsuarioSemNada:
    """Usuario ativo que nao possui permissao funcional alguma."""

    is_staff = False
    is_active = True

    def has_perm(self, _perm, obj=None) -> bool:
        return False


def test_quem_nao_tem_permissao_nenhuma_cai_numa_rota_aberta() -> None:
    """O destino do login nao pode mandar ninguem para uma porta fechada.

    `core.views.inicio_view` resolve para onde a pessoa pode ir. Se a varredura
    do menu devolvesse uma tela que ela nao pode abrir, o resultado seria o
    laco que essa view existe justamente para evitar -- e se devolvesse `None`
    sem tratamento, um erro. Hoje ela para em "Alterar senha", aberta a
    qualquer autenticado.
    """
    destino = primeira_tela_permitida(_UsuarioSemNada())

    assert destino is None or destino.lstrip("/") in ROTAS_ABERTAS, (
        f"Usuario sem permissao seria mandado para {destino!r}, que exige permissao."
    )


def test_o_destino_do_login_nao_e_uma_tela_fixa() -> None:
    """Regressao: enquanto era fixo, obrigava aquela tela a nao ter permissao.

    `LOGIN_REDIRECT_URL` apontava para `/reports/upcoming-movements/`, e por
    isso aquela tela nao podia exigir `reports.upcoming_movements.view` -- a
    chave ficava no catalogo sem guardar nada. O destino agora e derivado.
    """
    from django.conf import settings

    assert settings.LOGIN_REDIRECT_URL == "/inicio/", (
        "O destino do login voltou a ser uma tela concreta. Se for mesmo o "
        "caso, aquela tela precisa continuar exigindo a sua permissao -- e o "
        "laco de negacao precisa de outra saida."
    )
