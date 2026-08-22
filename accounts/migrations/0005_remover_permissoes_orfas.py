"""Remove tres permissoes que nao guardavam mais nada.

`transactions.cancel`, `transactions.close_month` e `transactions.reopen_month`
nao correspondem a endpoints do modulo. Fechar e reabrir mes usam
`settings.monthly_close.manage`; cancelamento de lancamento nao faz parte do
contrato atual.

As concessoes a usuarios somem junto, por `on_delete=CASCADE` em
`UserPermission.permission`. A reversao recria o catalogo, mas nao recupera
essas concessoes, que nao sao registradas antes da exclusao.
"""

from django.db import migrations

PERMISSOES_REMOVIDAS = (
    "transactions.cancel",
    "transactions.close_month",
    "transactions.reopen_month",
)

# Descricoes originais, para o caminho de volta. Vem de
# `0003_seed_permission_catalog`.
DESCRICOES = {
    "transactions.cancel": "Cancelar lançamentos",
    "transactions.close_month": "Fechar mês",
    "transactions.reopen_month": "Reabrir mês",
}


def remover(apps, _schema_editor):
    permissao = apps.get_model("accounts", "AppPermission")
    permissao.objects.filter(name__in=PERMISSOES_REMOVIDAS).delete()


def restaurar(apps, _schema_editor):
    """Recria as linhas do catalogo, NAO as concessoes.

    Quem tinha a permissao concedida nao a recupera: o `CASCADE` apagou os
    vinculos e esta migracao nao os registrou antes de apagar. Reverter aqui
    devolve apenas o catalogo; qualquer concessao precisa ser refeita
    explicitamente.
    """
    permissao = apps.get_model("accounts", "AppPermission")
    for nome in PERMISSOES_REMOVIDAS:
        permissao.objects.update_or_create(
            name=nome, defaults={"description": DESCRICOES[nome]}
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_rename_banking_permission_description")]

    operations = [migrations.RunPython(remover, restaurar)]
