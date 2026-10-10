"""Telas de cadastro: ordem da listagem e formulário de inclusão sob demanda.

Risco que protege: a lista sair na ordem do banco (que muda com o dado e com a
collation) em vez da que a pessoa lê, uma categoria aparecer fora do seu grupo,
o formulário de inclusão voltar a ocupar a tela o tempo todo, ou a linha de
links entre cadastros reaparecer (o menu já leva a cada um).
"""

import re
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from transactions.models import CashFlowCategory, CashFlowCategoryGroup


@pytest.fixture
def logado(client):
    user = get_user_model().objects.create_user(
        username="cadastros", password="senha-segura", user_type="administrator",
    )
    client.force_login(user)
    return client


def _ordem(html: str, nomes: list[str]) -> list[str]:
    """Os nomes na ordem em que aparecem como célula da tabela."""
    posicoes = {n: html.index(f">{n}</td>") for n in nomes}
    return sorted(nomes, key=posicoes.get)


@pytest.mark.django_db
def test_titulares_saem_por_nome_sem_diferenciar_maiusculas(logado):
    for nome in ("bruno", "Carlos", "Ana"):
        AccountOwner.objects.create(name=nome)

    html = logado.get(reverse("accounts:owners_view")).content.decode()

    assert _ordem(html, ["bruno", "Carlos", "Ana"]) == ["Ana", "bruno", "Carlos"]


@pytest.mark.django_db
def test_instituicoes_saem_por_tipo_e_depois_por_nome(logado):
    for nome, tipo in (("Alfa", "Corretora"), ("Zeta", "Banco"), ("beta", "Banco")):
        FinancialInstitution.objects.create(institution_name=nome, institution_type=tipo)

    html = logado.get(reverse("banking:institutions_view")).content.decode()

    assert _ordem(html, ["Alfa", "Zeta", "beta"]) == ["beta", "Zeta", "Alfa"]


@pytest.mark.django_db
def test_contas_saem_por_titular_instituicao_e_nome(logado):
    ana, bia = AccountOwner.objects.create(name="Ana"), AccountOwner.objects.create(name="Bia")
    banco_a = FinancialInstitution.objects.create(institution_name="Banco A", institution_type="Banco")
    banco_b = FinancialInstitution.objects.create(institution_name="Banco B", institution_type="Banco")
    for dono, banco, nome in (
        (bia, banco_a, "Conta Bia A"),
        (ana, banco_b, "Conta Ana B2"),
        (ana, banco_b, "conta ana b1"),
        (ana, banco_a, "Conta Ana A"),
    ):
        FinancialAccount.objects.create(
            owner=dono, institution=banco, account_name=nome,
            initial_balance=Decimal("0"), initial_balance_date=date(2026, 1, 1),
        )

    html = logado.get(reverse("banking:accounts_view")).content.decode()
    nomes = ["Conta Bia A", "Conta Ana B2", "conta ana b1", "Conta Ana A"]
    achados = sorted(nomes, key=lambda n: html.index(f"<td>{n}"))

    assert achados == ["Conta Ana A", "conta ana b1", "Conta Ana B2", "Conta Bia A"]


@pytest.mark.django_db
def test_categorias_saem_sob_o_grupo_e_as_sem_grupo_por_ultimo(logado):
    beta = CashFlowCategoryGroup.objects.create(group_name="Beta", position=1)
    alfa = CashFlowCategoryGroup.objects.create(group_name="Alfa", position=9)
    CashFlowCategory.objects.create(category_name="Zebra", group=alfa)
    CashFlowCategory.objects.create(category_name="abelha", group=alfa)
    CashFlowCategory.objects.create(category_name="Cavalo", group=beta)
    CashFlowCategory.objects.create(category_name="Solta")

    html = logado.get(reverse("transactions:categories_view")).content.decode()
    cabecalhos = re.findall(r'<th scope="rowgroup"[^>]*>([^<]+)<', html)
    categorias = ["abelha", "Zebra", "Cavalo", "Solta"]
    ordem = sorted(categorias, key=lambda n: html.index(f'category-indent">{n}</td>'))

    assert cabecalhos == ["Alfa", "Beta", "Sem grupo"]  # por nome do grupo, não pela posição
    assert ordem == ["abelha", "Zebra", "Cavalo", "Solta"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "rota",
    [
        "accounts:owners_view", "banking:institutions_view", "banking:accounts_view",
        "transactions:categories_view", "transactions:category_groups_view",
    ],
)
def test_incluir_fica_no_cabecalho_e_o_formulario_nasce_oculto(logado, rota):
    html = logado.get(reverse(rota)).content.decode()
    cabecalho = html[html.index('<header id="appPageHeader"'): html.index("</header>")]

    assert "data-cadastro-incluir" in cabecalho
    assert "data-cadastro-salvar" in cabecalho
    assert re.search(r'<button[^>]*data-cadastro-salvar[^>]*\shidden', cabecalho)
    assert re.search(r'<div id="cadastro-novo"[^>]*\shidden>', html)
    # O Salvar do cabeçalho envia o formulário que está na página.
    assert 'form="cadastro-form"' in cabecalho
    assert 'id="cadastro-form"' in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("rota", "url"),
    [
        ("accounts:owners_view", "/tables/owners/"),
        ("banking:institutions_view", "/tables/banks/"),
        ("banking:accounts_view", "/tables/accounts/"),
        ("transactions:categories_view", "/tables/categories/"),
        ("transactions:category_groups_view", "/tables/category-groups/"),
    ],
)
def test_nao_ha_linha_de_links_entre_cadastros_so_o_menu(logado, rota, url):
    html = logado.get(reverse(rota)).content.decode()
    miolo = html[html.index('<main id="appMain"'):]

    assert f'href="{url}"' not in miolo
    assert html.count(f'href="{url}"') == 1  # o item do menu lateral


@pytest.mark.django_db
def test_grupos_de_categoria_tem_tela_propria_com_a_contagem_de_categorias(logado):
    grupo = CashFlowCategoryGroup.objects.create(group_name="Moradia", position=1)
    CashFlowCategory.objects.create(category_name="Energia", group=grupo)
    CashFlowCategory.objects.create(category_name="Água", group=grupo)

    html = logado.get(reverse("transactions:category_groups_view")).content.decode()

    assert re.search(r"<td>Moradia</td>\s*<td>1</td>\s*<td>Sim</td>\s*<td>2</td>", html)
    assert "Nenhum grupo cadastrado" not in html
    # E a tela de Categorias já não carrega o cadastro de grupos.
    categorias = logado.get(reverse("transactions:categories_view")).content.decode()
    assert 'action="/tables/category-groups/create/"' not in categorias


@pytest.mark.django_db
def test_filtro_de_tipo_das_categorias_usa_os_mesmos_nomes_do_selo(logado):
    CashFlowCategory.objects.create(category_name="Mercado", kind="gerencial")
    CashFlowCategory.objects.create(category_name="Aplicação", kind="movimentacao")
    CashFlowCategory.objects.create(category_name="Entre contas", kind="transferencia")
    url = reverse("transactions:categories_view")

    def nomes(tipo):
        html = logado.get(url, {"filter_type": tipo}).content.decode()
        return {n for n in ("Mercado", "Aplicação", "Entre contas") if f'category-indent">{n}</td>' in html}

    assert nomes("gerencial") == {"Mercado"}
    assert nomes("movimentacao") == {"Aplicação"}
    assert nomes("transferencia") == {"Entre contas"}
    assert nomes("") == {"Mercado", "Aplicação", "Entre contas"}
    pagina = logado.get(url).content.decode()
    for rotulo in (">Gerencial</option>", ">Movimentação</option>", ">Transferência</option>"):
        assert rotulo in pagina
    assert ">Normal</option>" not in pagina and ">Interna</option>" not in pagina


@pytest.mark.django_db
def test_grupos_saem_na_ordem_dos_graficos_e_empatando_por_nome(logado):
    for nome, posicao in (("Beta", 2), ("alfa", 2), ("Zero", 1)):
        CashFlowCategoryGroup.objects.create(group_name=nome, position=posicao)

    html = logado.get(reverse("transactions:category_groups_view")).content.decode()

    assert _ordem(html, ["Beta", "alfa", "Zero"]) == ["Zero", "alfa", "Beta"]
