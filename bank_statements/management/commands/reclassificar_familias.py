"""Reclassifica as famílias determinísticas de lançamento (ver `bank_statements.familias`).

    python manage.py reclassificar_familias --usuario NOME
    python manage.py reclassificar_familias --usuario NOME --aplicar [--autorizar-meses]

Sem `--aplicar` é só a simulação: por família, quantos lançamentos, quanto
dinheiro, se a categoria e o grupo de destino seriam criados e quantos
meses-conta fechados seriam reabertos. Com `--aplicar`, tudo ou nada, com o
saldo de fechamento conferido antes de fechar de novo. Só muda lançamento que
está na categoria de origem da família. Faça o backup antes de aplicar em dados
reais (`python cli.py backup --projeto controle_bancario --tipos banco`).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from accounts.models import AppUser
from bank_statements import familias


class Command(BaseCommand):
    help = "Simula ou aplica a reclassificação das famílias determinísticas."

    def add_arguments(self, parser):
        parser.add_argument("--usuario", required=True, help="Usuário cujo acesso vale (e que assina a auditoria).")
        parser.add_argument("--aplicar", action="store_true", help="Grava; sem isto é só simulação.")
        parser.add_argument("--autorizar-meses", action="store_true", help="Permite reabrir e fechar de novo meses fechados.")

    def handle(self, *args, usuario, aplicar, autorizar_meses, **options):
        try:
            user = AppUser.objects.get(username=usuario)
        except AppUser.DoesNotExist as exc:
            raise CommandError(f"Usuário \"{usuario}\" não existe.") from exc

        for efeito in familias.simular(user):
            if not efeito.entradas:
                continue
            extras = []
            if efeito.categoria_nova:
                extras.append(f"cria a categoria \"{efeito.familia.para}\"")
            if efeito.grupo_novo:
                extras.append(f"cria o grupo \"{efeito.familia.grupo}\"")
            if efeito.meses_fechados:
                extras.append(f"reabre {efeito.meses_fechados} mês(es)-conta fechado(s)")
            sufixo = f" ({'; '.join(extras)})" if extras else ""
            self.stdout.write(
                f"{efeito.familia.nome}: {len(efeito.entradas)} lançamento(s), {efeito.total} "
                f"-> {efeito.familia.para}{sufixo}"
            )
        if not aplicar:
            self.stdout.write("Simulação: nada foi gravado. Use --aplicar para reclassificar.")
            return
        try:
            resultado = familias.aplicar(user, autorizar_meses=autorizar_meses)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{sum(resultado.values())} lançamento(s) reclassificado(s) em {len(resultado)} família(s).")
