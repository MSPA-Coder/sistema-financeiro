"""Remove quatro permissoes que nao guardavam mais nada.

Segunda leva do mesmo problema tratado em `0005_remover_permissoes_orfas`, e
pelo mesmo motivo: permissao que nao guarda nada e pior que inutil -- aparece
na tela de Permissoes como se fosse um controle, o administrador a desmarca
acreditando ter restringido algo, e nada muda.

As quatro, e por que cada uma ficou orfa:

- `settings.backup.run` e `settings.restore.run` perderam o que exigiam quando
  o backup local saiu deste projeto (ver `96536b5`, "retira o backup local,
  coberto pelo BackupRestore"). O que restou em Configuracoes · Banco de dados
  e a otimizacao, que tem chave propria.

- `settings.operations.audit` nunca teve rota, item de menu ou view. Consta do
  seed original (`0003_seed_permission_catalog`) e de nada mais.

- `transactions.transfer` e o caso do `reopen_month` da `0005` se repetindo:
  eram DUAS permissoes para a mesma operacao. Transferencia interna e criada
  por `transactions:transaction_new`, que exige `transactions.create`; nenhuma
  linha do projeto le `transactions.transfer`. Revoga-la nunca tirou de
  ninguem a capacidade de transferir.

Levantadas na revalidacao de 29/08/2026, que passou a varrer a URLconf e a
arvore de menu comparando com o catalogo -- ver
`tests/test_permissoes_por_rota.py`. A varredura e o que impede a terceira
leva: uma chave nova sem consumidor agora reprova a suite.

As concessoes a usuarios somem junto, por `on_delete=CASCADE` em
`UserPermission.permission`. E o desejado: guardar a concessao de uma permissao
inexistente so deixaria lixo referencial.
"""

from django.db import migrations

PERMISSOES_REMOVIDAS = (
    "settings.backup.run",
    "settings.restore.run",
    "settings.operations.audit",
    "transactions.transfer",
)

# Descricoes originais, para o caminho de volta. Vem de
# `0003_seed_permission_catalog`.
DESCRICOES = {
    "settings.backup.run": "Executar backup do banco de dados",
    "settings.restore.run": "Restaurar backup do banco de dados",
    "settings.operations.audit": "Auditar operações bancárias",
    "transactions.transfer": "Criar transferências internas",
}


def remover(apps, _schema_editor):
    permissao = apps.get_model("accounts", "AppPermission")
    permissao.objects.filter(name__in=PERMISSOES_REMOVIDAS).delete()


def restaurar(apps, _schema_editor):
    """Recria as linhas do catalogo, NAO as concessoes.

    Quem tinha a permissao concedida nao a recupera: o `CASCADE` apagou os
    vinculos e esta migracao nao os registrou antes de apagar. Reverter aqui
    devolve apenas o catalogo; qualquer concessao precisa ser refeita
    explicitamente. Mesmo contrato da `0005`.
    """
    permissao = apps.get_model("accounts", "AppPermission")
    for nome in PERMISSOES_REMOVIDAS:
        permissao.objects.update_or_create(
            name=nome, defaults={"description": DESCRICOES[nome]}
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_add_annual_planning_permission")]

    operations = [migrations.RunPython(remover, restaurar)]
