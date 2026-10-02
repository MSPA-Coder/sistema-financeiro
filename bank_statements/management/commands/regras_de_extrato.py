"""Regras explícitas de linha de extrato: lista e semeia as de uso comum.

    python manage.py regras_de_extrato listar
    python manage.py regras_de_extrato semear

`semear` é idempotente (pelo nome da regra) e descreve famílias, nunca ids: a
instituição, a categoria e a conta de destino são achadas pelo nome, e a regra
que depende de algo que ainda não existe é pulada com aviso. Isso permite rodar
o mesmo comando em cada ambiente.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from bank_statements.models import (
    RULE_ACTION_CATEGORY,
    RULE_ACTION_TRANSFER,
    RULE_SIGN_ANY,
    RULE_SIGN_CREDIT,
    StatementRule,
)
from banking.models import FinancialInstitution
from transactions.models import CashFlowCategory

# (nome, padrão, instituição, sinal, ação, categoria, conta de destino)
REGRAS_PADRAO = (
    (
        "Rende Fácil do BB é transferência para a conta Rende Fácil",
        "Rende Facil",
        "Banco do Brasil",
        RULE_SIGN_ANY,
        RULE_ACTION_TRANSFER,
        "",
        "Rende Fácil",
    ),
    (
        "Liberação de dinheiro do Mercado Pago é prêmio de loteria",
        "Liberacao de dinheiro",
        "Mercado Pago",
        RULE_SIGN_CREDIT,
        RULE_ACTION_CATEGORY,
        "Loterias",
        "",
    ),
)


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
        for nome, padrao, instituicao, sinal, acao_da_regra, categoria, destino in REGRAS_PADRAO:
            if StatementRule.objects.filter(name=nome).exists():
                continue
            inst = None
            if instituicao:
                inst = FinancialInstitution.objects.filter(institution_name__iexact=instituicao).first()
                if inst is None:
                    self.stderr.write(f"Pulada \"{nome}\": a instituição \"{instituicao}\" não existe.")
                    continue
            cat = None
            if categoria:
                cat = CashFlowCategory.objects.filter(category_name__iexact=categoria).first()
                if cat is None:
                    self.stderr.write(f"Pulada \"{nome}\": a categoria \"{categoria}\" não existe.")
                    continue
            StatementRule.objects.create(
                name=nome,
                pattern=padrao,
                institution=inst,
                sign=sinal,
                action=acao_da_regra,
                category=cat,
                destination_account_name=destino,
            )
            criadas += 1
        self.stdout.write(f"{criadas} regra(s) criada(s).")
