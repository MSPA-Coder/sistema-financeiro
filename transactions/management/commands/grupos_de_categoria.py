"""Cria os grupos de categoria aprovados em 02/10/2026 e liga a eles as categorias
existentes que ainda não têm grupo.

    python manage.py grupos_de_categoria semear

Idempotente, e **nunca troca o grupo de uma categoria que já tem um**: o que o
usuário escolheu na tela vale mais que esta tabela. Descreve famílias pelo nome,
nunca ids, para rodar igual em cada ambiente. A categoria que não existe é
pulada; a que existe e não está na tabela é listada, para ganhar um grupo à mão.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from transactions.models import CashFlowCategory, CashFlowCategoryGroup

# (nome, posição, aparece nos gráficos)
GRUPOS = (
    ("DBR", 10, True),
    ("Aposentadoria", 20, True),
    ("Imóvel Butantã", 30, True),
    ("Imóvel Jardim Iva", 40, True),
    ("Investimentos", 50, True),
    ("Moradia", 60, True),
    ("Alimentação", 70, True),
    ("Saúde", 80, True),
    ("Transporte", 90, True),
    ("Pessoal", 100, True),
    ("Lazer", 110, True),
    ("Viagem", 120, True),
    ("Trabalho e tecnologia", 130, True),
    ("Impostos", 140, True),
    ("Compras", 150, True),
    ("Bancos e cartões", 160, True),
    ("Outros", 170, True),
    ("Sistema", 900, False),
)

# categoria atual -> grupo. "Casa" mistura moradia e o aluguel do Butantã: fica
# em Moradia até a reclassificação separar o aluguel recebido em categoria própria.
CATEGORIA_PARA_GRUPO = {
    "DBR": "DBR",
    "INSS": "Aposentadoria",
    "Casa": "Moradia",
    "Jardim Iva": "Imóvel Jardim Iva",
    "Rendimentos": "Investimentos",
    "Aluguel de Açoes / Dividendos / JCP": "Investimentos",
    "Corretagem": "Investimentos",
    "Resultado em Bolsa": "Investimentos",
    "Alimentação": "Alimentação",
    "Supermercado": "Alimentação",
    "Saúde": "Saúde",
    "Veículos": "Transporte",
    "Pessoais - Roupas, beleza, etc": "Pessoal",
    "Educação": "Pessoal",
    "Lazer": "Lazer",
    "Loterias": "Lazer",
    "Viagem": "Viagem",
    "Custos Diversos Associados ao Trabalho": "Trabalho e tecnologia",
    "Impostos e Tributos": "Impostos",
    "Cartão de Crédito": "Bancos e cartões",
    "Outros": "Outros",
    "Ajustes de Saldo": "Sistema",
    "Aplicações": "Sistema",
    "Operações em Bolsa": "Sistema",
    "Transferência Entre Contas": "Sistema",
}


class Command(BaseCommand):
    help = "Cria os grupos de categoria e liga as categorias existentes sem grupo."

    def add_arguments(self, parser):
        parser.add_argument("acao", choices=("semear",))

    def handle(self, *args, **options):
        grupos = {}
        criados = 0
        for nome, posicao, nos_graficos in GRUPOS:
            grupo, novo = CashFlowCategoryGroup.objects.get_or_create(
                group_name=nome, defaults={"position": posicao, "in_charts": nos_graficos}
            )
            grupos[nome] = grupo
            criados += novo
        ligadas = 0
        for categoria in CashFlowCategory.objects.filter(group__isnull=True):
            nome_do_grupo = CATEGORIA_PARA_GRUPO.get(categoria.category_name)
            if nome_do_grupo is None:
                self.stderr.write(f"Sem grupo definido para a categoria \"{categoria.category_name}\".")
                continue
            categoria.group = grupos[nome_do_grupo]
            categoria.save(update_fields=["group", "updated_at"])
            ligadas += 1
        self.stdout.write(f"{criados} grupo(s) criado(s), {ligadas} categoria(s) ligada(s).")
