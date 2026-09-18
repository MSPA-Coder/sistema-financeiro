"""A rota que publica o caixa deste sistema para o consolidador.

ESTE ARQUIVO PROTEGE UM CONTRATO, NÃO UMA TELA

O que sai daqui é lido por outro deployable, escrito em outro momento, por
alguém que não vai ler este código. Contrato quebrado não dá erro: dá número
errado do outro lado. Por isso os testes falam da **forma** tanto quanto do
valor -- valor é texto e não número JSON, moeda é explícita, nada é somado
entre moedas, e o identificador de titular e instituição é o nome normalizado,
que é o que os dois sistemas compartilham.

E a rota carrega o saldo de todas as contas: os testes de autenticação são
sobre isso, não sobre formalidade.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core import patrimonio
from core.domain.finance import ENTRY_TYPE_EXPENSE, ENTRY_TYPE_INCOME, STATUS_REALIZED
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

TOKEN = "token-de-teste-com-mais-de-trinta-e-dois-caracteres"
ROTA = "/patrimonio/v1/resumo"


@pytest.fixture
def contas():
    owner = AccountOwner.objects.create(name="Mariano")
    banco = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    corretora = FinancialInstitution.objects.create(
        institution_name="Avenue", institution_type="Corretora"
    )
    em_reais = FinancialAccount.objects.create(
        owner=owner, institution=banco, account_name="Conta corrente",
        initial_balance=Decimal("100.00"), currency="BRL",
        initial_balance_date=date(2025, 12, 31),
    )
    em_dolar = FinancialAccount.objects.create(
        owner=owner, institution=corretora, account_name="Conta em dólar",
        initial_balance=Decimal("1250.00"), currency="USD",
        initial_balance_date=date(2025, 12, 31),
    )
    categoria = CashFlowCategory.objects.create(category_name="Salário")
    CashFlowEntry.objects.create(
        account=em_reais, category=categoria, entry_type=ENTRY_TYPE_INCOME,
        description="Salário", entry_amount=Decimal("900.00"),
        due_date=date(2026, 1, 10), realized_date=date(2026, 1, 10),
        realized_amount=Decimal("900.00"), status=STATUS_REALIZED,
    )
    CashFlowEntry.objects.create(
        account=em_reais, category=categoria, entry_type=ENTRY_TYPE_EXPENSE,
        description="Conta de luz", entry_amount=Decimal("50.00"),
        due_date=date(2026, 2, 10), realized_date=date(2026, 2, 10),
        realized_amount=Decimal("50.00"), status=STATUS_REALIZED,
    )
    return {"em_reais": em_reais, "em_dolar": em_dolar}


@pytest.fixture
def com_token(monkeypatch, tmp_path):
    """Concede o token pela forma que vale em produção: arquivo montado.

    Sob o Compose, `REQUIRE_FILE_SECRETS=true` recusa a variável direta. Um
    teste que só exercitasse a variável passaria aqui e a rota responderia 503
    no servidor, que é o pior lugar para descobrir isso.
    """
    arquivo = tmp_path / "patrimonio_token"
    arquivo.write_text(TOKEN, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(arquivo))
    return TOKEN


def pedir(token: str | None = TOKEN, **parametros):
    cabecalhos = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token is not None else {}
    return Client().get(ROTA, parametros, **cabecalhos)


# --- A chave é a permissão -------------------------------------------------


def test_sem_token_nao_ha_resposta(contas, com_token):
    resposta = pedir(token=None)

    assert resposta.status_code == 401
    assert "contas" not in resposta.json()


def test_token_errado_nao_ha_resposta(contas, com_token):
    resposta = pedir(token="token-errado-mas-do-mesmo-tamanho-que-o-certo")

    assert resposta.status_code == 401
    assert "contas" not in resposta.json()


def test_sem_segredo_configurado_a_rota_nao_atende(contas, monkeypatch):
    """"Não configurado" não é "não autorizado".

    Responder 401 aqui mandaria o operador procurar por horas um token errado
    que, na verdade, nunca foi concedido a este servidor.
    """
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.delenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", raising=False)

    resposta = pedir()

    assert resposta.status_code == 503


def test_segredo_curto_demais_e_tratado_como_ausente(contas, monkeypatch, tmp_path):
    """Um token de oito letras não protege saldo de conta nenhuma."""
    arquivo = tmp_path / "patrimonio_token"
    arquivo.write_text("curtinho", encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(arquivo))

    resposta = pedir(token="curtinho")

    assert resposta.status_code == 503


def test_sob_o_compose_a_variavel_direta_nao_concede_o_token(contas, monkeypatch):
    """`REQUIRE_FILE_SECRETS=true` é o contrato do Compose, e vale aqui também.

    Sem esta trava, uma sobra de `PATRIMONIO_TOKEN` no ambiente do processo
    substituiria em silêncio o segredo montado como arquivo -- e ninguém
    descobriria qual dos dois está valendo.
    """
    monkeypatch.setenv("REQUIRE_FILE_SECRETS", "true")
    monkeypatch.setenv(patrimonio.NOME_DO_SEGREDO, TOKEN)
    monkeypatch.delenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", raising=False)

    resposta = pedir()

    assert resposta.status_code == 503


def test_a_resposta_nao_pode_ser_guardada_por_intermediario(contas, com_token):
    resposta = pedir()

    assert resposta["Cache-Control"] == "no-store"


def test_a_rota_so_responde_a_get(contas, com_token):
    resposta = Client().post(ROTA, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    assert resposta.status_code == 405


# --- O contrato ------------------------------------------------------------


def test_o_resumo_traz_saldo_por_conta_e_total_por_moeda(contas, com_token):
    resposta = pedir(data="2026-03-01")

    corpo = resposta.json()
    assert resposta.status_code == 200
    assert corpo["contrato"] == "patrimonio/v1"
    assert corpo["sistema"] == "controle-bancario"
    assert corpo["data_de_referencia"] == "2026-03-01"

    por_id = {linha["nome"]: linha for linha in corpo["contas"]}
    assert por_id["Conta corrente"]["saldo"] == "950.00"
    assert por_id["Conta corrente"]["moeda"] == "BRL"
    assert por_id["Conta em dólar"]["saldo"] == "1250.00"
    assert por_id["Conta em dólar"]["moeda"] == "USD"

    assert corpo["totais_por_moeda"] == [
        {"moeda": "BRL", "total": "950.00", "linhas": 1},
        {"moeda": "USD", "total": "1250.00", "linhas": 1},
    ]


def test_nenhum_valor_viaja_como_numero_json(contas, com_token):
    """`float` não representa 0,10, e quem consolida somaria centavos que não existem."""
    bruto = json.loads(pedir(data="2026-03-01").content)

    for linha in bruto["contas"]:
        assert isinstance(linha["saldo"], str)
    for total in bruto["totais_por_moeda"]:
        assert isinstance(total["total"], str)


def test_titular_e_instituicao_sao_identificados_pelo_nome_normalizado(contas, com_token):
    """O id do banco só vale dentro de um sistema; o nome vale nos dois."""
    corpo = pedir(data="2026-03-01").json()

    assert corpo["titulares"] == [{"id": "mariano", "nome": "Mariano"}]
    assert {i["id"] for i in corpo["instituicoes"]} == {"c6", "avenue"}
    assert {linha["titular"] for linha in corpo["contas"]} == {"mariano"}


def test_a_conta_leva_id_prefixado_pelo_sistema(contas, com_token):
    corpo = pedir(data="2026-03-01").json()

    ids = {linha["id"] for linha in corpo["contas"]}
    assert ids == {
        f"controle-bancario:conta:{contas['em_reais'].id}",
        f"controle-bancario:conta:{contas['em_dolar'].id}",
    }


def test_a_conta_leva_a_propria_pagina_na_data_pedida(contas, com_token):
    """O caminho é relativo: o endereço público é de quem consome."""
    corpo = pedir(data="2026-03-01").json()

    enderecos = {linha["id"]: linha["endereco"] for linha in corpo["contas"]}
    em_reais = f"controle-bancario:conta:{contas['em_reais'].id}"
    assert enderecos[em_reais] == (
        f"/banking/accounts/{contas['em_reais'].id}/?data=2026-03-01"
    )


def test_o_endereco_publicado_abre_a_pagina_da_conta(contas, com_token):
    """O caminho é o que a tela de fato entende, e não uma aproximação dele."""
    corpo = pedir(data="2026-03-01").json()
    em_reais = f"controle-bancario:conta:{contas['em_reais'].id}"
    endereco = next(linha["endereco"] for linha in corpo["contas"] if linha["id"] == em_reais)
    usuario = get_user_model().objects.create_user(
        username="dono", password="troca-esta-senha-no-primeiro-acesso",
        user_type=USER_TYPE_ADMINISTRATOR,
    )
    navegador = Client()
    navegador.force_login(usuario)

    resposta = navegador.get(endereco)

    assert resposta.status_code == 200
    assert resposta.context["account"] == contas["em_reais"]
    assert resposta.context["selected_period"] == "2026-03"
    assert resposta.context["reference_date"] == date(2026, 3, 1)


def test_o_contrato_traz_as_listas_que_o_outro_sistema_preenche(contas, com_token):
    """Lista vazia diz "não tenho nada a dizer"; chave ausente não diz nada."""
    corpo = pedir(data="2026-03-01").json()

    assert corpo["posicoes"] == []
    assert corpo["proventos"] == []
    assert corpo["ativos_alternativos"] == []


def test_moedas_nunca_sao_somadas_entre_si(contas, com_token):
    corpo = pedir(data="2026-03-01").json()

    moedas = [total["moeda"] for total in corpo["totais_por_moeda"]]
    assert moedas == sorted(set(moedas))
    assert len(moedas) == 2


# --- A foto tem data -------------------------------------------------------


def test_a_data_pedida_muda_o_saldo(contas, com_token):
    """Em 15/01 o salário já entrou e a conta de luz ainda não saiu."""
    corpo = pedir(data="2026-01-15").json()

    por_nome = {linha["nome"]: linha for linha in corpo["contas"]}
    assert por_nome["Conta corrente"]["saldo"] == "1000.00"


def test_conta_que_ainda_nao_existia_fica_fora_da_foto(contas, com_token):
    """É o que a data no saldo inicial (U04a) comprou.

    Sem ela, o saldo inicial de uma conta aberta em 2026 seria somado a uma
    foto de 2025 -- dinheiro aparecendo num passado em que ele não estava.
    """
    corpo = pedir(data="2025-06-30").json()

    assert corpo["contas"] == []
    assert corpo["totais_por_moeda"] == []


def test_data_invalida_e_recusada(contas, com_token):
    resposta = pedir(data="30/06/2026")

    assert resposta.status_code == 400
    assert "contas" not in resposta.json()


def test_sem_data_a_foto_e_de_hoje(contas, com_token):
    from django.utils import timezone

    corpo = pedir().json()

    assert corpo["data_de_referencia"] == timezone.localdate().isoformat()


# --- O vocabulário ---------------------------------------------------------


@pytest.mark.parametrize(
    ("nome", "esperado"),
    [
        ("Mercado Pago", "mercado-pago"),
        ("mercado  pago", "mercado-pago"),
        ("Itaú", "itau"),
        ("Banco do Brasil", "banco-do-brasil"),
        ("C6", "c6"),
    ],
)
def test_identidade_normaliza_o_nome(nome, esperado):
    assert patrimonio.identidade(nome) == esperado
