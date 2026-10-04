"""Estrutura e comportamento do menu Movimentação (sem banco).

O que pode quebrar sem ninguém notar: um grupo aceso em tela que não é dele (o
prefixo `/banking/` do antigo Banking acenderia o grupo em Reclassificação),
uma tela de detalhe que não realça item nenhum, e um grupo sem permissão
nenhuma que vira link morto. A árvore é estrutura declarada, e o contexto do
menu lê só `user.has_perm`; por isso nada aqui abre o banco.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.context_processors import _build_menu_items, _serialize_menu


def _usuario(*permissoes: str):
    liberadas = set(permissoes)
    return SimpleNamespace(has_perm=lambda chave: chave in liberadas, is_staff=False)


def _todos():
    def chaves(itens):
        for item in itens:
            if item.required_permission:
                yield item.required_permission
            yield from chaves(item.children)

    return _usuario(*chaves(_build_menu_items()))


def _item(menu, *rotulos):
    """Desce a árvore serializada pelos rótulos; `None` se algum não existir."""
    atual = menu
    for rotulo in rotulos:
        achado = next((i for i in atual if i["label"] == rotulo), None)
        if achado is None:
            return None
        atual, item = achado["children"], achado
    return item


def _menu(caminho: str, usuario=None):
    return _serialize_menu(_build_menu_items(), usuario or _todos(), caminho)


def test_movimentacao_na_ordem_pedida_e_sem_o_nivel_banking():
    menu = _menu("/dashboard/")
    movimentacao = _item(menu, "Movimentação")

    assert [i["label"] for i in movimentacao["children"]] == [
        "Lançamentos",
        "Saldo Aplicações",
        "Importação",
        "Reclassificação",
        "Fechamento Mensal",
        "Parcelas e Recorrências",
        "Comprovantes",
    ]
    assert [i["label"] for i in _item(menu, "Movimentação", "Importação")["children"]] == [
        "Importar extratos e faturas",
        "Conciliação",
        "Extratos importados",
        "Faturas e projeção",
        "Situação das Contas",
    ]
    assert _item(menu, "Movimentação", "Banking") is None


def test_grupo_importacao_nao_acende_em_tela_que_nao_e_dele():
    for caminho in ("/banking/reclassification/", "/banking/balance/", "/banking/attachments/"):
        menu = _menu(caminho)
        assert not _item(menu, "Movimentação", "Importação")["active"], caminho
        assert _item(menu, "Movimentação")["active"], caminho


def test_tela_de_detalhe_realca_o_item_da_lista_e_acende_o_grupo():
    fatura = _menu("/banking/import/12/fatura/")
    assert _item(fatura, "Movimentação", "Importação", "Faturas e projeção")["active"]
    assert not _item(fatura, "Movimentação", "Importação", "Extratos importados")["active"]
    assert _item(fatura, "Movimentação", "Importação")["active"]

    extrato = _menu("/banking/import/12/extrato/")
    assert _item(extrato, "Movimentação", "Importação", "Extratos importados")["active"]
    assert not _item(extrato, "Movimentação", "Importação", "Faturas e projeção")["active"]


def test_etapas_da_importacao_realcam_importar_extratos_e_faturas():
    for caminho in ("/banking/imports/", "/banking/imports/confirm/"):
        menu = _menu(caminho)
        assert _item(menu, "Movimentação", "Importação", "Importar extratos e faturas")["active"], caminho


def test_grupo_sem_filho_permitido_some_em_vez_de_virar_link_morto():
    so_lancamentos = _menu("/dashboard/", _usuario("transactions.view"))
    movimentacao = _item(so_lancamentos, "Movimentação")
    assert [i["label"] for i in movimentacao["children"]] == ["Lançamentos"]

    sem_nada = _menu("/dashboard/", _usuario())
    assert _item(sem_nada, "Movimentação") is None
    assert _item(sem_nada, "Relatórios") is None


def test_quem_so_consulta_enxerga_o_grupo_importacao_com_as_telas_de_leitura():
    menu = _menu("/dashboard/", _usuario("banking.view"))

    assert [i["label"] for i in _item(menu, "Movimentação", "Importação")["children"]] == [
        "Extratos importados",
        "Faturas e projeção",
        "Situação das Contas",
    ]
