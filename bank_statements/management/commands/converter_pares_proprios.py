"""Converte em transferência os pares de receita e despesa entre contas do
próprio titular (ver `bank_statements.pares_proprios`).

    python manage.py converter_pares_proprios --usuario NOME [--desde AAAA-MM-DD]
    python manage.py converter_pares_proprios --usuario NOME --aplicar [--autorizar-meses]

Sem `--aplicar` é só a simulação: lista os pares e não grava nada. Com
`--aplicar`, tudo ou nada. Mês fechado só é reaberto com `--autorizar-meses` e
fecha de novo com o mesmo saldo, ou nada é gravado. Faça o backup do banco
antes de aplicar em dados reais (`python cli.py backup --projeto
controle_bancario --tipos banco`).
"""
from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from accounts.models import AppUser
from bank_statements import pares_proprios


class Command(BaseCommand):
    help = "Simula ou converte pares próprios de receita e despesa em transferência."

    def add_arguments(self, parser):
        parser.add_argument("--usuario", required=True, help="Usuário cujo acesso vale (e que assina a auditoria).")
        parser.add_argument("--desde", help="Só pares realizados a partir desta data (AAAA-MM-DD).")
        parser.add_argument("--aplicar", action="store_true", help="Grava; sem isto é só simulação.")
        parser.add_argument("--autorizar-meses", action="store_true", help="Permite reabrir e fechar de novo meses fechados.")

    def handle(self, *args, usuario, desde, aplicar, autorizar_meses, **options):
        try:
            user = AppUser.objects.get(username=usuario)
        except AppUser.DoesNotExist as exc:
            raise CommandError(f"Usuário \"{usuario}\" não existe.") from exc
        try:
            inicio = date.fromisoformat(desde) if desde else None
        except ValueError as exc:
            raise CommandError("--desde deve ser AAAA-MM-DD.") from exc

        pares = pares_proprios.encontrar_pares(user, desde=inicio)
        total = sum((par.valor for par in pares), 0)
        for par in pares:
            self.stdout.write(
                f"{par.saida.realized_date:%d/%m/%Y} {par.valor:>10} "
                f"{par.saida.account} -> {par.entrada.account}  "
                f"[{par.saida.category.category_name} / {par.entrada.category.category_name}]"
            )
        self.stdout.write(f"{len(pares)} par(es), volume {total}.")
        if not aplicar:
            self.stdout.write("Simulação: nada foi gravado. Use --aplicar para converter.")
            return
        try:
            convertidos = pares_proprios.aplicar(user, pares, autorizar_meses=autorizar_meses)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{convertidos} par(es) convertido(s) em transferência.")
