"""Aplica um plano de ajustes de dados (ver `bank_statements.ajustes`).

    python manage.py aplicar_ajustes --usuario NOME --arquivo plano.json
    python manage.py aplicar_ajustes --usuario NOME --arquivo - --aplicar < plano.json

Sem `--aplicar` roda tudo dentro de uma transação revertida: o relatório mostra
o que seria feito e recusa exatamente o que a aplicação recusaria. Com um erro
nada é gravado. O plano descreve dinheiro real e fica fora do repositório.
Backup antes de aplicar em dados reais (`python cli.py backup --projeto
controle_bancario --tipos banco`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from accounts.models import AppUser
from bank_statements import ajustes


class Command(BaseCommand):
    help = "Simula ou aplica um plano de ajustes de dados (JSON)."

    def add_arguments(self, parser):
        parser.add_argument("--usuario", required=True)
        parser.add_argument("--arquivo", required=True, help="Caminho do plano JSON, ou - para a entrada padrão.")
        parser.add_argument("--aplicar", action="store_true", help="Grava; sem isto é só simulação.")

    def handle(self, *args, usuario, arquivo, aplicar, **options):
        try:
            user = AppUser.objects.get(username=usuario)
        except AppUser.DoesNotExist as exc:
            raise CommandError(f"Usuário \"{usuario}\" não existe.") from exc
        try:
            texto = sys.stdin.read() if arquivo == "-" else Path(arquivo).read_text(encoding="utf-8")
            plano = json.loads(texto)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Não consegui ler o plano: {exc}") from exc
        try:
            relatorio = ajustes.executar(user, plano, aplicar=aplicar)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for linha in relatorio.linhas:
            self.stdout.write(linha)
        for erro in relatorio.erros:
            self.stderr.write(f"ERRO {erro}")
        if relatorio.erros:
            raise CommandError(f"{len(relatorio.erros)} erro(s): nada foi gravado.")
        self.stdout.write("Aplicado." if aplicar else "Simulação: nada foi gravado.")
