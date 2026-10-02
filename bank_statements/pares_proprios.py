"""Converte em transferência os pares de lançamentos que já foram gravados como
receita e despesa entre contas do próprio titular.

Antes de o extrato reconhecer transferências (`extrato`), um Pix da Genial para o
C6 do mesmo titular entrava como despesa em "Outros" numa conta e como receita
em "Outros" na outra, inflando as duas pontas do mês. Aqui a simulação acha esses
pares e a aplicação os transforma numa transferência só, **sem mudar valor, data,
conta nem a conciliação com o extrato**: só a natureza (categoria de
transferência, operação interna e vínculo entre as pontas). Por isso o saldo de
cada conta e o de cada mês fechado ficam exatamente como estavam, e é isso que
autoriza reabrir o mês: o saldo de fechamento é conferido antes de fechar de novo.

Só entra no par o lançamento realizado, avulso, gerencial, de contas diferentes,
na mesma moeda, de sinais opostos, mesmo valor, até dois dias de distância, com o
nome de um titular no texto (ou cara de transferência nas duas pontas), e só
quando o par é único dos dois lados.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import uuid4

from django.db import transaction as db_transaction

from accounts.services import can_use_transfer_destination
from banking.services import accessible_account_ids
from core.domain.finance import (
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    OPERATION_SINGLE,
    STATUS_REALIZED,
)
from transactions.models import (
    BankOperation,
    CashFlowCategory,
    CashFlowEntry,
)
from transactions.services import _account_label

from .extrato import nomes_dos_titulares, texto_cita_titular, texto_tem_cara_de_transferencia
from .meses import meses_reabertos

MOTIVO_DA_REABERTURA = "Conversão de pares próprios em transferência"
_JANELA = timedelta(days=2)
_LIMITE = 5000


@dataclass
class Par:
    saida: CashFlowEntry
    entrada: CashFlowEntry

    @property
    def valor(self):
        return self.saida.realized_amount or self.saida.entry_amount

    @property
    def meses(self) -> set[tuple[int, int, int]]:
        return {
            (entrada.account_id, entrada.realized_date.year, entrada.realized_date.month)
            for entrada in (self.saida, self.entrada)
        }


def _candidatos(user, desde: date | None):
    contas = accessible_account_ids(user, "update")
    consulta = (
        CashFlowEntry.objects.filter(
            account_id__in=contas,
            status=STATUS_REALIZED,
            operation_type=OPERATION_SINGLE,
            is_recurring=False,
            source_entry__isnull=True,
            category__kind=CATEGORY_KIND_MANAGERIAL,
        )
        .exclude(account__account_kind="cartao_credito")
        .select_related("account__owner", "account__institution", "category")
        .order_by("realized_date", "id")
    )
    if desde is not None:
        consulta = consulta.filter(realized_date__gte=desde)
    return list(consulta[:_LIMITE])


def encontrar_pares(user, *, desde: date | None = None) -> list[Par]:
    """Os pares convertíveis, sem gravar nada."""
    candidatos = _candidatos(user, desde)
    padroes = nomes_dos_titulares()
    por_valor: dict = defaultdict(list)
    for entrada in candidatos:
        por_valor[(entrada.realized_amount or entrada.entry_amount)].append(entrada)

    def vizinhas(entrada):
        achadas = []
        for outra in por_valor.get(entrada.realized_amount or entrada.entry_amount, ()):
            if outra.id == entrada.id or outra.account_id == entrada.account_id:
                continue
            if outra.entry_type == entrada.entry_type:
                continue
            if outra.account.currency != entrada.account.currency:
                continue
            if abs(outra.realized_date - entrada.realized_date) > _JANELA:
                continue
            indicio = (
                texto_cita_titular(entrada.description, padroes)
                or texto_cita_titular(outra.description, padroes)
                or (
                    texto_tem_cara_de_transferencia(entrada.description)
                    and texto_tem_cara_de_transferencia(outra.description)
                )
            )
            if indicio and can_use_transfer_destination(
                user, (entrada if entrada.entry_type == ENTRY_TYPE_INCOME else outra).account_id
            ):
                achadas.append(outra)
        return achadas

    pares: list[Par] = []
    usados: set[int] = set()
    for entrada in candidatos:
        if entrada.id in usados or entrada.entry_type != ENTRY_TYPE_EXPENSE:
            continue
        achadas = vizinhas(entrada)
        if len(achadas) != 1:
            continue
        outra = achadas[0]
        de_volta = vizinhas(outra)
        if len(de_volta) == 1 and de_volta[0].id == entrada.id and outra.id not in usados:
            pares.append(Par(saida=entrada, entrada=outra))
            usados.update({entrada.id, outra.id})
    return pares


def _converter(user, par: Par, categoria: CashFlowCategory, audit_context) -> None:
    from core.services import log_audit_event

    operacao = BankOperation.objects.create(
        operation_key=f"{OPERATION_INTERNAL_TRANSFER}-{uuid4().hex}",
        operation_type=OPERATION_INTERNAL_TRANSFER,
        description="",
        status=STATUS_REALIZED,
        installment_total=1,
        responsible_user=user,
    )
    saida, entrada = par.saida, par.entrada
    antes = {
        saida.id: (saida.category.category_name, saida.description),
        entrada.id: (entrada.category.category_name, entrada.description),
    }
    saida.category = categoria
    saida.operation_type = OPERATION_INTERNAL_TRANSFER
    saida.bank_operation = operacao
    saida.description = f"Conta Destino: {_account_label(entrada.account)}"
    saida.save(update_fields=["category", "operation_type", "bank_operation", "description", "updated_at"])
    entrada.category = categoria
    entrada.operation_type = OPERATION_INTERNAL_TRANSFER
    entrada.bank_operation = operacao
    entrada.source_entry = saida
    entrada.description = f"Conta Origem: {_account_label(saida.account)}"
    entrada.save(
        update_fields=["category", "operation_type", "bank_operation", "source_entry", "description", "updated_at"]
    )
    for item in (saida, entrada):
        categoria_antiga, descricao_antiga = antes[item.id]
        log_audit_event(
            "cash_flow_entry", item.id, "update",
            old_values={"category": categoria_antiga, "description": descricao_antiga},
            new_values={"category": categoria.category_name, "description": item.description},
            user=user, request_context=audit_context,
            summary="Par de lançamentos próprios convertido em transferência.",
        )


def aplicar(user, pares: list[Par], *, autorizar_meses: bool = False, audit_context=None) -> int:
    """Converte os pares, tudo ou nada. Devolve quantos pares foram convertidos."""
    if not pares:
        return 0
    categoria = CashFlowCategory.objects.filter(kind=CATEGORY_KIND_TRANSFER).order_by("id").first()
    if categoria is None:
        raise ValueError("Não há categoria de transferência cadastrada.")
    meses = sorted({mes for par in pares for mes in par.meses})
    with db_transaction.atomic(), meses_reabertos(
        user, meses, autorizar=autorizar_meses, motivo=MOTIVO_DA_REABERTURA, audit_context=audit_context
    ):
        for par in pares:
            _converter(user, par, categoria, audit_context)
    return len(pares)
