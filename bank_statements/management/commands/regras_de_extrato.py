"""Regras explícitas de linha de extrato: lista e semeia as de uso comum.

    python manage.py regras_de_extrato listar
    python manage.py regras_de_extrato semear

`semear` é idempotente (pelo nome da regra) e descreve famílias, nunca ids: a
instituição, a categoria e a conta de destino são achadas pelo nome, e a regra
que depende de algo que ainda não existe é pulada com aviso. Isso permite rodar
o mesmo comando em cada ambiente. Cada instituição e cada categoria aceita
mais de um nome (a primeira que existir vale), porque o mesmo cadastro tem
grafias diferentes entre ambientes.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand

from bank_statements.models import (
    RULE_ACTION_CATEGORY,
    RULE_ACTION_TRANSFER,
    RULE_SIGN_ANY,
    RULE_SIGN_CREDIT,
    RULE_SIGN_DEBIT,
    StatementRule,
)
from banking.models import FinancialInstitution
from transactions.models import CashFlowCategory

CORRETORA_XP = ("SCP XP Investimentos", "SCP XP Investimestos", "XP Investimentos")


@dataclass(frozen=True)
class Regra:
    nome: str
    padrao: str
    instituicoes: tuple[str, ...]
    sinal: str
    acao: str
    categorias: tuple[str, ...] = ()
    conta_de_destino: str = ""
    instituicao_de_destino: tuple[str, ...] = ()


REGRAS_PADRAO = (
    Regra(
        "Rende Fácil do BB é transferência para a conta Rende Fácil", "Rende Facil",
        ("Banco do Brasil",), RULE_SIGN_ANY, RULE_ACTION_TRANSFER, conta_de_destino="Rende Fácil",
    ),
    Regra(
        "Liberação de dinheiro do Mercado Pago é prêmio de loteria", "Liberacao de dinheiro",
        ("Mercado Pago",), RULE_SIGN_CREDIT, RULE_ACTION_CATEGORY, categorias=("Loterias",),
    ),
    Regra(
        "Tesouro Direto na corretora XP é transferência para a conta Tesouro Direto", "TESOURO DIRETO",
        CORRETORA_XP, RULE_SIGN_ANY, RULE_ACTION_TRANSFER,
        conta_de_destino="Tesouro Direto", instituicao_de_destino=("XP",),
    ),
    Regra(
        "Juros sobre capital na corretora XP", "JUROS S/ CAPITAL", CORRETORA_XP, RULE_SIGN_CREDIT,
        RULE_ACTION_CATEGORY, categorias=("Aluguel de Açoes / Dividendos / JCP", "Aluguel de Açoes", "Rendimentos"),
    ),
    Regra(
        "Dividendos na corretora XP", "DIVIDENDOS DE CLIENTES", CORRETORA_XP, RULE_SIGN_CREDIT,
        RULE_ACTION_CATEGORY, categorias=("Aluguel de Açoes / Dividendos / JCP", "Aluguel de Açoes", "Rendimentos"),
    ),
    Regra(
        "Resgate de fundo na corretora XP é movimentação", "RESGATE TREND INVESTBACK", CORRETORA_XP,
        RULE_SIGN_CREDIT, RULE_ACTION_CATEGORY, categorias=("Aplicações",),
    ),
    Regra(
        "Aplicação em fundo na corretora XP é movimentação", "APLICACAO FUNDOS", CORRETORA_XP,
        RULE_SIGN_DEBIT, RULE_ACTION_CATEGORY, categorias=("Aplicações",),
    ),
    Regra(
        "IRRF do resgate de fundo na corretora XP", "IRRF S/RESGATE FUNDOS", CORRETORA_XP,
        RULE_SIGN_DEBIT, RULE_ACTION_CATEGORY, categorias=("IRRF sobre investimentos", "Impostos e Tributos"),
    ),
    Regra(
        "Investback (cashback) na corretora XP", "INVESTBACK", CORRETORA_XP, RULE_SIGN_CREDIT,
        RULE_ACTION_CATEGORY, categorias=("Cartão de Crédito",),
    ),
)


def _instituicao(nomes: tuple[str, ...]) -> FinancialInstitution | None:
    for nome in nomes:
        achada = FinancialInstitution.objects.filter(institution_name__iexact=nome).first()
        if achada is not None:
            return achada
    return None


def _categoria(nomes: tuple[str, ...]) -> CashFlowCategory | None:
    for nome in nomes:
        achada = CashFlowCategory.objects.filter(category_name__iexact=nome).first()
        if achada is not None:
            return achada
    return None


class Command(BaseCommand):
    help = "Lista ou semeia as regras explícitas de linha de extrato."

    def add_arguments(self, parser):
        parser.add_argument("acao", choices=("listar", "semear"))

    def handle(self, *args, acao, **options):
        if acao == "listar":
            for regra in StatementRule.objects.select_related("institution", "category"):
                estado = "ativa" if regra.active else "inativa"
                self.stdout.write(f"[{estado}] {regra.name} :: \"{regra.pattern}\" -> {regra.action}")
            return
        criadas = 0
        for regra in REGRAS_PADRAO:
            if StatementRule.objects.filter(name=regra.nome).exists():
                continue
            instituicao = _instituicao(regra.instituicoes)
            if instituicao is None:
                self.stderr.write(f"Pulada \"{regra.nome}\": nenhuma das instituições {regra.instituicoes} existe.")
                continue
            categoria = None
            if regra.categorias:
                categoria = _categoria(regra.categorias)
                if categoria is None:
                    self.stderr.write(f"Pulada \"{regra.nome}\": nenhuma das categorias {regra.categorias} existe.")
                    continue
            destino = None
            if regra.instituicao_de_destino:
                destino = _instituicao(regra.instituicao_de_destino)
                if destino is None:
                    self.stderr.write(
                        f"Pulada \"{regra.nome}\": a instituição de destino {regra.instituicao_de_destino} não existe."
                    )
                    continue
            StatementRule.objects.create(
                name=regra.nome,
                pattern=regra.padrao,
                institution=instituicao,
                sign=regra.sinal,
                action=regra.acao,
                category=categoria,
                destination_account_name=regra.conta_de_destino,
                destination_institution=destino,
            )
            criadas += 1
        self.stdout.write(f"{criadas} regra(s) criada(s).")
