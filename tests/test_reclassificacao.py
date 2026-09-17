"""O comando que reclassifica lançamentos por regra.

O QUE ESTE ARQUIVO PROTEGE

Reclassificar é reescrever o passado: 96 lançamentos mudaram de categoria de uma
vez, 26 meses fechados foram reabertos e fechados de novo, e 17 contrapartes
nasceram. Um erro aqui não aparece na tela no dia seguinte -- aparece meses
depois, num relatório que deixou de bater com o extrato.

Daí as três perguntas que os testes fazem, e que são as três promessas do
comando:

1. **ele move o significado, nunca o dinheiro.** Saldo de conta e saldo de
   fechamento têm de sair idênticos do outro lado;
2. **ele não adivinha.** Descrição que nenhuma família descreve fica exatamente
   onde está, e é relatada. Em particular, uma categoria não muda de tipo
   enquanto houver lançamento indeciso dentro dela -- trocar o tipo tiraria esse
   lançamento do resultado sem que ninguém tivesse decidido isso;
3. **ele pode rodar de novo.** É o mesmo comando que vai rodar na base de
   produção, onde a primeira tentativa pode parar no meio.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    OPERATION_SINGLE,
    STATUS_REALIZED,
)
from transactions.models import AccountMonthClose, CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario():
    """Uma conta, seu veículo de aplicação, uma conta em dólar e as categorias.

    É o retrato mínimo da base real: dinheiro que vai para um CDB, dinheiro que
    vira dólar, e dinheiro que só estava na categoria errada.
    """
    user = AppUser.objects.create_user(username="operador", password="senha-bem-segura")
    owner = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(
        user=user, owner=owner, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="Banco", institution_type="Banco")
    corretora = FinancialInstitution.objects.create(
        institution_name="Avenue", institution_type="Corretora"
    )
    conta = FinancialAccount.objects.create(
        owner=owner, institution=banco, account_name="Conta corrente",
        initial_balance=Decimal("1000.00"), currency="BRL",
    )
    veiculo = FinancialAccount.objects.create(
        owner=owner, institution=banco, account_name="CDB",
        initial_balance=Decimal("0.00"), currency="BRL",
    )
    em_dolar = FinancialAccount.objects.create(
        owner=owner, institution=corretora, account_name="Conta em dólar",
        initial_balance=Decimal("0.00"), currency="USD",
    )
    save_transfer_destination_accesses(user, {veiculo.id, em_dolar.id})

    categorias = {
        "Aplicações": CashFlowCategory.objects.create(
            category_name="Aplicações", kind=CATEGORY_KIND_MANAGERIAL
        ),
        "Operações em Bolsa": CashFlowCategory.objects.create(
            category_name="Operações em Bolsa", kind=CATEGORY_KIND_MANAGERIAL
        ),
        "Rendimentos": CashFlowCategory.objects.create(
            category_name="Rendimentos", kind=CATEGORY_KIND_MANAGERIAL
        ),
        "Corretagem": CashFlowCategory.objects.create(
            category_name="Corretagem", kind=CATEGORY_KIND_MANAGERIAL
        ),
        "Transferência Entre Contas": CashFlowCategory.objects.create(
            category_name="Transferência Entre Contas", kind=CATEGORY_KIND_TRANSFER
        ),
    }
    return {
        "user": user,
        "conta": conta,
        "veiculo": veiculo,
        "em_dolar": em_dolar,
        "categorias": categorias,
    }


def lancar(cenario, descricao, *, categoria="Aplicações", tipo=ENTRY_TYPE_INCOME,
           valor="100.00", dia=date(2026, 3, 10)):
    return CashFlowEntry.objects.create(
        account=cenario["conta"],
        category=cenario["categorias"][categoria],
        entry_type=tipo,
        description=descricao,
        entry_amount=Decimal(valor),
        due_date=dia,
        realized_date=dia,
        realized_amount=Decimal(valor),
        status=STATUS_REALIZED,
        operation_type=OPERATION_SINGLE,
    )


def reclassificar(cenario, *, aplicar=False, arquivo=None):
    saida = StringIO()
    argumentos = ["reclassificar_lancamentos", "--usuario", cenario["user"].username]
    if aplicar:
        argumentos.append("--aplicar")
    if arquivo is not None:
        argumentos += ["--valores-em-moeda", str(arquivo)]
    call_command(*argumentos, stdout=saida)
    return saida.getvalue()


def saldo(conta: FinancialAccount) -> Decimal:
    conta.refresh_from_db()
    total = conta.initial_balance
    for entry in CashFlowEntry.objects.filter(account=conta, status=STATUS_REALIZED):
        total += entry.entry_amount if entry.entry_type == ENTRY_TYPE_INCOME else -entry.entry_amount
    return total


def arquivo_de_valores(tmp_path, linhas: str):
    caminho = tmp_path / "valores.csv"
    caminho.write_text(linhas, encoding="utf-8")
    return caminho


# --- Simular é o padrão ----------------------------------------------------


def test_sem_aplicar_nada_e_gravado(cenario):
    entry = lancar(cenario, "Conta Remunerada")

    relatorio = reclassificar(cenario)

    entry.refresh_from_db()
    assert entry.category.category_name == "Aplicações"
    assert CashFlowEntry.objects.count() == 1
    assert "SIMULAÇÃO" in relatorio
    assert "Rendimentos" in relatorio


# --- Move o significado, não o dinheiro ------------------------------------


def test_familia_gerencial_so_troca_de_categoria(cenario):
    entry = lancar(cenario, "Conta Remunerada", valor="210.67")
    antes = saldo(cenario["conta"])

    reclassificar(cenario, aplicar=True)

    entry.refresh_from_db()
    assert entry.category.category_name == "Rendimentos"
    assert entry.entry_type == ENTRY_TYPE_INCOME
    assert entry.entry_amount == Decimal("210.67")
    assert entry.operation_type == OPERATION_SINGLE
    assert saldo(cenario["conta"]) == antes


def test_familia_de_veiculo_vira_par_de_transferencia(cenario):
    """O dinheiro que saiu da conta passa a aparecer onde ele está."""
    entry = lancar(cenario, "CDB comodidade 102%", tipo=ENTRY_TYPE_EXPENSE, valor="9400.00")
    saldo_da_conta = saldo(cenario["conta"])

    reclassificar(cenario, aplicar=True)

    entry.refresh_from_db()
    assert entry.category.kind == CATEGORY_KIND_TRANSFER
    assert entry.operation_type == OPERATION_INTERNAL_TRANSFER

    contraparte = CashFlowEntry.objects.get(source_entry=entry)
    assert contraparte.account_id == cenario["veiculo"].id
    assert contraparte.entry_type == ENTRY_TYPE_INCOME
    assert contraparte.entry_amount == Decimal("9400.00")
    assert contraparte.realized_amount == Decimal("9400.00")
    assert contraparte.bank_operation_id == entry.bank_operation_id
    # A conta de origem não sente nada: o lançamento dela continua lá, com o
    # mesmo sinal e o mesmo valor. Quem ganha saldo é o veículo, que antes não
    # existia em lugar nenhum.
    assert saldo(cenario["conta"]) == saldo_da_conta
    assert saldo(cenario["veiculo"]) == Decimal("9400.00")


def test_a_descricao_do_extrato_sobrevive_nas_duas_pontas(cenario):
    """Ela é a única ligação entre o lançamento e a linha do extrato que o gerou.

    A tela, ao criar uma transferência nova, escreve "Conta Destino: ..." nas
    descrições. Aqui isso apagaria a origem bancária do lançamento -- e a conta
    destino não se perde, porque o par é estrutural, não textual.
    """
    entry = lancar(cenario, "Resgate CDB no vencimento", valor="28241.18")

    reclassificar(cenario, aplicar=True)

    entry.refresh_from_db()
    contraparte = CashFlowEntry.objects.get(source_entry=entry)
    assert entry.description == "Resgate CDB no vencimento"
    assert contraparte.description == "Resgate CDB no vencimento"


# --- Entre moedas: quem diz quanto entrou é o extrato -----------------------


def test_entre_moedas_sem_o_valor_do_destino_nao_grava(cenario):
    entry = lancar(cenario, "Avenue - Compra US$", tipo=ENTRY_TYPE_EXPENSE, valor="5420.00")

    relatorio = reclassificar(cenario, aplicar=True)

    entry.refresh_from_db()
    assert entry.category.category_name == "Aplicações"
    assert entry.operation_type == OPERATION_SINGLE
    assert CashFlowEntry.objects.count() == 1
    assert "PENDENTES" in relatorio
    assert "valor creditado no destino" in relatorio


def test_entre_moedas_usa_o_valor_informado_no_destino(cenario, tmp_path):
    entry = lancar(
        cenario, "Avenue - Compra US$", tipo=ENTRY_TYPE_EXPENSE, valor="5420.00",
        dia=date(2026, 3, 19),
    )
    valores = arquivo_de_valores(tmp_path, "# comentário\n2026-03-19;5420,00;1030,67\n")

    reclassificar(cenario, aplicar=True, arquivo=valores)

    entry.refresh_from_db()
    contraparte = CashFlowEntry.objects.get(source_entry=entry)
    assert entry.entry_amount == Decimal("5420.00")
    assert contraparte.account_id == cenario["em_dolar"].id
    assert contraparte.entry_amount == Decimal("1030.67")
    assert contraparte.realized_amount == Decimal("1030.67")


def test_cada_compra_consome_a_sua_linha_do_extrato(cenario, tmp_path):
    """Duas compras do mesmo valor no mesmo dia são duas operações, não uma.

    Se a linha do extrato não fosse consumida, a segunda compra reaproveitaria a
    taxa da primeira -- e o par de valores pareceria certo sem nunca ter
    existido.
    """
    primeira = lancar(
        cenario, "Avenue - Compra US$", tipo=ENTRY_TYPE_EXPENSE, valor="5420.00",
        dia=date(2026, 3, 19),
    )
    segunda = lancar(
        cenario, "Avenue - Compra US$", tipo=ENTRY_TYPE_EXPENSE, valor="5420.00",
        dia=date(2026, 3, 19),
    )
    valores = arquivo_de_valores(
        tmp_path, "2026-03-19;5420,00;1030,67\n2026-03-19;5420,00;1033,56\n"
    )

    reclassificar(cenario, aplicar=True, arquivo=valores)

    creditados = sorted(
        CashFlowEntry.objects.filter(source_entry__in=[primeira.id, segunda.id]).values_list(
            "entry_amount", flat=True
        )
    )
    assert creditados == [Decimal("1030.67"), Decimal("1033.56")]


def test_arquivo_de_valores_malformado_para_o_comando(cenario, tmp_path):
    valores = arquivo_de_valores(tmp_path, "2026-03-19;5420,00\n")

    with pytest.raises(CommandError, match="esperava"):
        reclassificar(cenario, arquivo=valores)


# --- Não adivinha ----------------------------------------------------------


def test_descricao_desconhecida_fica_onde_esta(cenario):
    entry = lancar(cenario, "Aporte que ninguém descreveu")

    relatorio = reclassificar(cenario, aplicar=True)

    entry.refresh_from_db()
    assert entry.category.category_name == "Aplicações"
    assert "nenhuma família descreve" in relatorio


def test_categoria_nao_muda_de_tipo_com_lancamento_indeciso_dentro(cenario):
    """A trava que impede a reclassificação silenciosa.

    Trocar o tipo de "Operações em Bolsa" para movimentação tira do resultado
    TODO lançamento que restar nela. Se um deles não casou com nenhuma família,
    ele sairia do resultado de carona -- sem que ninguém tivesse decidido isso.
    """
    lancar(cenario, "Operações Bolsa D+2 Pr 20/03/2026", categoria="Operações em Bolsa")
    lancar(cenario, "Algo que nenhuma regra conhece", categoria="Operações em Bolsa")

    relatorio = reclassificar(cenario, aplicar=True)

    categoria = CashFlowCategory.objects.get(category_name="Operações em Bolsa")
    assert categoria.kind == CATEGORY_KIND_MANAGERIAL
    assert "NÃO muda de tipo" in relatorio


def test_categoria_muda_de_tipo_quando_tudo_nela_foi_decidido(cenario):
    lancar(cenario, "Operações Bolsa D+2 Pr 20/03/2026", categoria="Operações em Bolsa")
    lancar(
        cenario, "Operações Bm&F Pr 20/03/2026", categoria="Operações em Bolsa",
        tipo=ENTRY_TYPE_EXPENSE, valor="0.50",
    )

    reclassificar(cenario, aplicar=True)

    categoria = CashFlowCategory.objects.get(category_name="Operações em Bolsa")
    assert categoria.kind == CATEGORY_KIND_MOVEMENT
    assert categoria.transactions.count() == 1


def test_conta_destino_sem_concessao_nao_recebe_transferencia(cenario):
    save_transfer_destination_accesses(cenario["user"], set())
    entry = lancar(cenario, "CDB comodidade 102%", tipo=ENTRY_TYPE_EXPENSE, valor="9400.00")

    relatorio = reclassificar(cenario, aplicar=True)

    entry.refresh_from_db()
    assert entry.operation_type == OPERATION_SINGLE
    assert "não está concedida como destino" in relatorio


def test_categoria_de_destino_ausente_vira_pendencia(cenario):
    CashFlowCategory.objects.filter(category_name="Rendimentos").delete()
    entry = lancar(cenario, "Conta Remunerada")

    relatorio = reclassificar(cenario, aplicar=True)

    entry.refresh_from_db()
    assert entry.category.category_name == "Aplicações"
    assert "não existe" in relatorio


# --- Mês fechado -----------------------------------------------------------


def test_mes_fechado_e_reaberto_e_refeito_com_o_mesmo_saldo(cenario):
    entry = lancar(cenario, "Conta Remunerada", dia=date(2026, 3, 10), valor="210.67")
    fechamento = AccountMonthClose.objects.create(
        account=cenario["conta"], year=2026, month=3,
        closing_balance=Decimal("1210.67"),
        closed_at=timezone.now(), closed_by_user=cenario["user"],
    )

    reclassificar(cenario, aplicar=True)

    entry.refresh_from_db()
    fechamento.refresh_from_db()
    assert entry.category.category_name == "Rendimentos"
    assert fechamento.active is True
    assert fechamento.closing_balance == Decimal("1210.67")
    assert fechamento.reopen_reason == ""
    assert AccountMonthClose.objects.filter(active=True).count() == 1


def test_mes_fechado_sem_mudanca_nenhuma_nao_e_reaberto(cenario):
    """Reabrir um mês é um ato registrado. Não se faz por precaução."""
    lancar(cenario, "Nada que alguma regra conheça", dia=date(2026, 3, 10))
    fechamento = AccountMonthClose.objects.create(
        account=cenario["conta"], year=2026, month=3,
        closing_balance=Decimal("1100.00"),
        closed_at=timezone.now(), closed_by_user=cenario["user"],
    )
    fechado_em = fechamento.closed_at

    reclassificar(cenario, aplicar=True)

    fechamento.refresh_from_db()
    assert fechamento.closed_at == fechado_em
    assert fechamento.reopened_at is None


# --- Roda de novo ----------------------------------------------------------


def test_rodar_duas_vezes_nao_muda_nada(cenario, tmp_path):
    lancar(cenario, "Conta Remunerada")
    lancar(cenario, "CDB comodidade 102%", tipo=ENTRY_TYPE_EXPENSE, valor="9400.00")
    lancar(cenario, "Operações Bolsa D+2 Pr 20/03/2026", categoria="Operações em Bolsa")
    lancar(
        cenario, "Avenue - Compra US$", tipo=ENTRY_TYPE_EXPENSE, valor="5420.00",
        dia=date(2026, 3, 19),
    )
    valores = arquivo_de_valores(tmp_path, "2026-03-19;5420,00;1030,67\n")

    reclassificar(cenario, aplicar=True, arquivo=valores)
    retrato = sorted(
        CashFlowEntry.objects.values_list(
            "id", "account_id", "category_id", "entry_type", "entry_amount", "operation_type"
        )
    )
    saldos = {conta.id: saldo(conta) for conta in FinancialAccount.objects.all()}

    relatorio = reclassificar(cenario, aplicar=True, arquivo=valores)

    assert sorted(
        CashFlowEntry.objects.values_list(
            "id", "account_id", "category_id", "entry_type", "entry_amount", "operation_type"
        )
    ) == retrato
    assert {conta.id: saldo(conta) for conta in FinancialAccount.objects.all()} == saldos
    assert "Nada a fazer" in relatorio


def test_usuario_inexistente_para_o_comando(cenario):
    with pytest.raises(CommandError, match="não existe"):
        call_command("reclassificar_lancamentos", "--usuario", "ninguem", stdout=StringIO())
