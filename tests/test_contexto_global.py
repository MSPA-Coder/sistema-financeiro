"""Titular, instituição e conta acompanham o menu (decisão de 09/10/2026).

O repasse acontece no clique do link (`application.js`); aqui ficam o mapa de
telas que o JavaScript recebe e a faixa "Mostrando só..." que mostra o
recorte e o limpa.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from core.contexto_global import PARAMETROS_DE_CONTEXTO, faixa_de_contexto, telas_com_contexto


def test_mapa_de_telas_cobre_as_telas_do_seletor_e_respeita_o_destino():
    telas = telas_com_contexto()
    for caminho in ("/transactions/", "/dashboard/", "/reports/projections/",
                    "/reports/upcoming-movements/", "/management/"):
        assert telas[caminho] == list(PARAMETROS_DE_CONTEXTO), caminho
    # Posição por conta recorta por titular e instituição, não por conta.
    assert telas["/reports/account-position/"] == ["owner_id", "institution_id"]
    # Telas com seletor próprio ficam de fora.
    assert "/reports/annual-planning/" not in telas
    assert "/settings/monthly-close/" not in telas


def test_tela_sem_contexto_nao_mostra_faixa():
    request = RequestFactory().get("/reports/annual-planning/?owner_id=1")
    assert faixa_de_contexto(request, user=object())["global_context_labels"] == []


def _resolvido(url, user):
    """Como as telas fazem: `selected_context` valida e anota no request."""
    from reports.services import selected_context

    request = RequestFactory().get(url)
    selected_context(user, request.GET, request=request)
    return request


@pytest.fixture
def cenario(db):
    from accounts.models import AccountOwner
    from banking.models import FinancialAccount, FinancialInstitution

    user = get_user_model().objects.create_user(
        username="contexto-global", password="senha-segura", user_type="administrator",
    )
    dono = AccountOwner.objects.create(name="Titular do recorte")
    conta = FinancialAccount.objects.create(
        owner=dono,
        institution=FinancialInstitution.objects.create(institution_name="Banco recorte", institution_type="Banco"),
        account_name="Conta recorte",
    )
    return user, dono, conta


@pytest.mark.django_db
def test_faixa_mostra_o_recorte_e_o_link_que_o_limpa(cenario):
    user, dono, conta = cenario
    request = _resolvido(f"/transactions/?owner_id={dono.id}&account_id={conta.id}&period=2026-11", user)

    faixa = faixa_de_contexto(request, user)

    assert faixa["global_context_labels"] == ["Titular do recorte", "Banco recorte / Conta recorte"]
    limpo = parse_qs(urlsplit(faixa["global_context_reset_url"]).query, keep_blank_values=True)
    # os três vão vazios (o repasse no clique não os devolve) e o resto fica
    assert limpo == {"owner_id": [""], "institution_id": [""], "account_id": [""], "period": ["2026-11"]}


@pytest.mark.django_db
def test_titular_que_o_usuario_nao_ve_nao_vira_nome_na_faixa(cenario):
    from accounts.models import AccountOwner

    _admin, _dono, _conta = cenario
    outro = AccountOwner.objects.create(name="Titular alheio")
    restrito = get_user_model().objects.create_user(
        username="sem-acesso", password="senha-segura", user_type="user",
    )
    request = _resolvido(f"/transactions/?owner_id={outro.id}", restrito)

    assert "Titular alheio" not in faixa_de_contexto(request, restrito)["global_context_labels"]


@pytest.mark.django_db
def test_lancamentos_com_recorte_mostram_a_faixa(client, cenario):
    user, dono, _conta = cenario
    client.force_login(user)

    response = client.get(f"/transactions/?owner_id={dono.id}")

    assert response.status_code == 200
    assert b"data-global-context" in response.content
    assert b"Titular do recorte" in response.content
