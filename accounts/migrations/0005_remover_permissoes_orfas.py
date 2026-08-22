"""Remove tres permissoes que nao guardavam mais nada.

`transactions.cancel`, `transactions.close_month` e `transactions.reopen_month`
eram exigidas apenas pelas tres rotas POST orfas removidas em 2026-08-22 (ver
`transactions/urls.py`). Depois daquela remocao, ficaram no catalogo sem nada
para autorizar -- e permissao que nao guarda nada e pior que inutil: aparece na
tela de permissoes como se fosse um controle.

O caso de `reopen_month` era o que incomodava. Reabrir mes continua possivel
por `core:settings_reopen_month`, que exige `settings.monthly_close.manage`.
Eram DUAS permissoes para a mesma operacao, e um administrador que revogasse
`transactions.reopen_month` acreditando ter tirado a capacidade nao teria
tirado nada.

`transactions.cancel` cai por outro motivo: cancelar lancamento deixou de
existir como funcionalidade (ver `transactions/migrations/0002`).

As concessoes a usuarios somem junto, por `on_delete=CASCADE` em
`UserPermission.permission`. Isso e desejado: manter a concessao de uma
permissao inexistente so deixaria lixo referencial. O perfil "gestor", unico
que as concedia, foi ajustado em `accounts/services.py`.
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
    devolve o catalogo ao estado anterior, e a concessao teria de ser refeita
    a mao -- o que e aceitavel porque nenhuma das tres autorizava coisa alguma
    quando foram removidas.
    """
    permissao = apps.get_model("accounts", "AppPermission")
    for nome in PERMISSOES_REMOVIDAS:
        permissao.objects.update_or_create(
            name=nome, defaults={"description": DESCRICOES[nome]}
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_rename_banking_permission_description")]

    operations = [migrations.RunPython(remover, restaurar)]
