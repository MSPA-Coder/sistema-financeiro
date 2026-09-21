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
from core.domain.finance import (
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

TOKEN = "token-de-teste-com-mais-de-trinta-e-dois-caracteres"
ROTA = "/patrimonio/v1/resumo"
ROTA_V2 = "/patrimonio/v2/resumo"
ROTA_V3_ATIVIDADES = "/patrimonio/v3/activities"
ROTA_V3_CATEGORIAS = "/patrimonio/v3/categories"
ROTA_V3_METADATA = "/patrimonio/v3/metadata"


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


def pedir_v2(token: str | None = TOKEN, **parametros):
    cabecalhos = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token is not None else {}
    return Client().get(ROTA_V2, parametros, **cabecalhos)


def pedir_v3(rota, token: str | None = TOKEN, **parametros):
    cabecalhos = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token is not None else {}
    return Client().get(rota, parametros, **cabecalhos)


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


# --- A versão de fluxos ----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_v2_preserva_envelope_e_usa_o_mesmo_bearer(contas, com_token):
    resposta = pedir_v2(data="2026-03-01", inicio="2026-01-01")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["contrato"] == "patrimonio/v2"
    assert corpo["data_de_referencia"] == "2026-03-01"
    assert corpo["periodo_dos_fluxos"] == {
        "inicio": "2026-01-01",
        "fim": "2026-03-01",
        "criterio": "realizado",
        "granularidade": "dia",
    }
    assert {"contas", "totais_por_moeda", "fluxos"} <= corpo.keys()
    assert resposta["Cache-Control"] == "no-store"


@pytest.mark.django_db(transaction=True)
def test_v2_sem_bearer_nao_publica_dados(contas, com_token):
    resposta = pedir_v2(token=None, data="2026-03-01")

    assert resposta.status_code == 401
    assert "contas" not in resposta.json()


@pytest.mark.django_db(transaction=True)
def test_v2_so_responde_a_get(contas, com_token):
    resposta = Client().post(ROTA_V2, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    assert resposta.status_code == 405


@pytest.mark.django_db(transaction=True)
def test_v2_agrega_realizados_por_data_moeda_e_natureza(contas, com_token):
    categoria_transferencia = CashFlowCategory.objects.create(
        category_name="Transferência v2", kind=CATEGORY_KIND_TRANSFER
    )
    categoria_movimentacao = CashFlowCategory.objects.create(
        category_name="Movimentação v2", kind=CATEGORY_KIND_MOVEMENT
    )
    CashFlowEntry.objects.create(
        account=contas["em_reais"], category=categoria_transferencia,
        entry_type=ENTRY_TYPE_EXPENSE, description="Envio", entry_amount=Decimal("30.00"),
        due_date=date(2026, 2, 15), realized_date=date(2026, 2, 15),
        realized_amount=Decimal("30.00"), status=STATUS_REALIZED,
    )
    CashFlowEntry.objects.create(
        account=contas["em_reais"], category=categoria_movimentacao,
        entry_type=ENTRY_TYPE_INCOME, description="Resgate", entry_amount=Decimal("7.00"),
        due_date=date(2026, 2, 15), realized_date=date(2026, 2, 15),
        realized_amount=Decimal("7.00"), status=STATUS_REALIZED,
    )
    # Lançamento em aberto e fora do intervalo não podem viajar para a v2.
    CashFlowEntry.objects.create(
        account=contas["em_reais"], category=categoria_movimentacao,
        entry_type=ENTRY_TYPE_INCOME, description="Projetado", entry_amount=Decimal("999.00"),
        due_date=date(2026, 2, 15), status=STATUS_PROJECTED,
    )

    corpo = pedir_v2(data="2026-02-15", inicio="2026-02-15").json()
    fluxos = {(f["moeda"], f["natureza"]): f for f in corpo["fluxos"]}
    assert fluxos[("BRL", "transferencia")] == {
        "data": "2026-02-15", "moeda": "BRL", "natureza": "transferencia",
        "entradas": "0.00", "saidas": "30.00", "liquido": "-30.00", "linhas": 1,
    }
    assert fluxos[("BRL", "movimentacao")] == {
        "data": "2026-02-15", "moeda": "BRL", "natureza": "movimentacao",
        "entradas": "7.00", "saidas": "0.00", "liquido": "7.00", "linhas": 1,
    }


@pytest.mark.django_db(transaction=True)
def test_v2_publica_saldo_inicial_do_periodo_como_ajuste_de_base(contas, com_token):
    FinancialAccount.objects.create(
        owner=contas["em_reais"].owner, institution=contas["em_reais"].institution,
        account_name="Conta nova v2", currency="BRL", initial_balance=Decimal("-20.00"),
        initial_balance_date=date(2026, 2, 1),
    )

    corpo = pedir_v2(data="2026-02-01", inicio="2026-02-01").json()

    assert corpo["fluxos"] == [{
        "data": "2026-02-01", "moeda": "BRL", "natureza": "ajuste_de_base",
        "entradas": "0.00", "saidas": "20.00", "liquido": "-20.00", "linhas": 1,
    }]


@pytest.mark.django_db(transaction=True)
def test_v2_agrega_no_banco_sem_misturar_moedas_ou_valor_previsto(contas, com_token):
    categoria = CashFlowCategory.objects.create(category_name="Gerencial USD v2")
    transferencia_legada = CashFlowCategory.objects.create(
        category_name="Transferência por operação v2"
    )
    for valor in (Decimal("11.11"), Decimal("1.11")):
        CashFlowEntry.objects.create(
            account=contas["em_dolar"], category=categoria,
            entry_type=ENTRY_TYPE_INCOME, description="Receita USD",
            entry_amount=Decimal("99.00"), due_date=date(2026, 2, 20),
            realized_date=date(2026, 2, 20), realized_amount=valor,
            status=STATUS_REALIZED,
        )
    CashFlowEntry.objects.create(
        account=contas["em_reais"], category=transferencia_legada,
        entry_type=ENTRY_TYPE_EXPENSE, description="Transferência antiga",
        entry_amount=Decimal("25.00"), due_date=date(2026, 2, 20),
        realized_date=date(2026, 2, 20), realized_amount=Decimal("20.00"),
        status=STATUS_REALIZED, operation_type=OPERATION_INTERNAL_TRANSFER,
    )

    corpo = pedir_v2(data="2026-02-20", inicio="2026-02-20").json()
    fluxos = {(item["moeda"], item["natureza"]): item for item in corpo["fluxos"]}

    assert fluxos[("USD", "gerencial")]["entradas"] == "12.22"
    assert fluxos[("USD", "gerencial")]["linhas"] == 2
    assert fluxos[("BRL", "transferencia")]["saidas"] == "20.00"
    assert len(fluxos) == 2


@pytest.mark.parametrize(
    ("parametros", "mensagem"),
    [
        ({"data": "2026-01-01", "inicio": "2026-01-02"}, "inicio"),
        ({"data": "2036-01-03", "inicio": "2026-01-01"}, "limite"),
        ({"data": "01/02/2026"}, "data"),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_v2_valida_intervalo_e_datas(contas, com_token, parametros, mensagem):
    resposta = pedir_v2(**parametros)

    assert resposta.status_code == 400
    assert mensagem in resposta.json()["erro"]


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


# --- A extensão somente leitura v3 ----------------------------------------


@pytest.mark.django_db(transaction=True)
def test_v3_atividades_sao_detalhadas_ordenadas_e_tem_ids_opacos(contas, com_token):
    resposta = pedir_v3(ROTA_V3_ATIVIDADES, page_size=10)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["contrato"] == "patrimonio/v3"
    assert corpo["recurso"] == "atividades"
    assert [item["descricao"] for item in corpo["itens"]] == ["Conta de luz", "Salário"]
    item = corpo["itens"][0]
    assert item["id"].startswith("controle-bancario:atividade:")
    assert not item["id"].endswith(str(contas["em_reais"].id))
    assert item["conta"]["id"].startswith("controle-bancario:conta:")
    assert item["categoria"]["natureza"] == "gerencial"
    assert item["deep_link"].startswith("/transaction/")
    # O link é local e não uma URL arbitrária fornecida por dados externos.
    assert item["conta"]["deep_link"].startswith("/banking/accounts/")


@pytest.mark.django_db(transaction=True)
def test_v3_atividades_tem_paginacao_deterministica(contas, com_token):
    primeira = pedir_v3(ROTA_V3_ATIVIDADES, page_size=1, page=1).json()
    segunda = pedir_v3(ROTA_V3_ATIVIDADES, page_size=1, page=2).json()

    assert primeira["paginacao"] == {
        "pagina": 1,
        "tamanho": 1,
        "total": 2,
        "paginas": 2,
        "tem_anterior": False,
        "tem_proxima": True,
        "anterior": None,
        "proxima": "/patrimonio/v3/activities?page=2&page_size=1",
    }
    assert segunda["itens"][0]["id"] != primeira["itens"][0]["id"]
    assert segunda["paginacao"]["tem_anterior"] is True


@pytest.mark.django_db(transaction=True)
def test_v3_categorias_e_metadata_publicam_so_o_que_existe(contas, com_token):
    categorias = pedir_v3(ROTA_V3_CATEGORIAS).json()
    metadata = pedir_v3(ROTA_V3_METADATA).json()

    assert categorias["recurso"] == "categorias"
    assert [item["nome"] for item in categorias["itens"]] == ["Salário"]
    assert categorias["itens"][0]["id"].startswith("controle-bancario:categoria:")
    assert categorias["itens"][0]["deep_link"] == "/tables/categories/"
    assert metadata["capacidades"]["escrita"] is False
    assert metadata["capacidades"]["fluxos"] is True
    assert metadata["capacidades"]["renda"] is False
    assert metadata["capacidades"]["performance"] is False
    assert metadata["capacidades"]["eventos"] is False
    assert metadata["moedas"] == ["BRL", "USD"]
    assert len(metadata["contas"]) == 2
    assert len(metadata["categorias"]) == 1
    assert metadata["periodo_disponivel"] == {
        "inicio": "2026-01-10",
        "fim": "2026-02-10",
    }


def test_v3_categoria_tolera_timestamp_legado_ausente(contas):
    categoria = CashFlowCategory.objects.first()
    categoria.created_at = None
    categoria.updated_at = None

    payload = patrimonio._categoria_v3(categoria)

    assert payload["criada_em"] is None
    assert payload["atualizada_em"] is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("rota", [ROTA_V3_ATIVIDADES, ROTA_V3_CATEGORIAS, ROTA_V3_METADATA])
def test_v3_exige_bearer_e_so_responde_a_get(contas, com_token, rota):
    sem_token = Client().get(rota)
    post = Client().post(rota, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    assert sem_token.status_code == 401
    assert post.status_code == 405


@pytest.mark.django_db(transaction=True)
def test_v3_filtra_por_id_opaco_e_recusa_referencia_desconhecida(contas, com_token):
    metadata = pedir_v3(ROTA_V3_METADATA).json()
    conta_id = metadata["contas"][0]["id"]
    resposta = pedir_v3(ROTA_V3_ATIVIDADES, conta=conta_id)

    assert resposta.status_code == 200
    assert all(item["conta"]["id"] == conta_id for item in resposta.json()["itens"])
    assert pedir_v3(ROTA_V3_ATIVIDADES, conta="controle-bancario:conta:desconhecida").status_code == 400
