from django.db import migrations


PERMISSIONS = (
    ("dashboard.view", "Visualizar Dashboard"),
    ("transactions.view", "Visualizar lançamentos"),
    ("transactions.create", "Incluir lançamentos"),
    ("transactions.update", "Editar lançamentos"),
    ("transactions.realize", "Marcar lançamentos como realizados"),
    ("transactions.cancel", "Cancelar lançamentos"),
    ("transactions.delete", "Excluir lançamentos"),
    ("transactions.transfer", "Criar transferências internas"),
    ("transactions.close_month", "Fechar mês"),
    ("transactions.reopen_month", "Reabrir mês"),
    ("projections.view", "Visualizar Projeções"),
    ("reports.upcoming_movements.view", "Visualizar Próximos movimentos"),
    ("reports.account_position.view", "Visualizar Posição por conta"),
    ("management.view", "Visualizar Gestão"),
    ("management.manage", "Gerenciar Gestão (tags, projetos, orçamento)"),
    ("tables.view", "Visualizar Cadastros"),
    ("tables.owners.manage", "Gerenciar titulares"),
    ("tables.institutions.manage", "Gerenciar instituições financeiras"),
    ("tables.accounts.manage", "Gerenciar contas"),
    ("tables.categories.manage", "Gerenciar categorias"),
    ("banking.view", "Visualizar módulo Bancos"),
    ("banking.import", "Importar extratos bancários"),
    ("banking.reconcile", "Conciliar extratos bancários"),
    ("banking.attachments.manage", "Gerenciar comprovantes de lançamentos"),
    ("operations.view", "Visualizar Operações (lançamentos agrupados)"),
    ("settings.view", "Visualizar Configurações"),
    ("settings.theme.update", "Atualizar perfil e visibilidade de contas"),
    ("settings.audit.view", "Visualizar trilha de auditoria"),
    ("settings.monthly_close.manage", "Fechar/reabrir mês pela tela de Configurações"),
    ("settings.database.optimize", "Executar diagnóstico e otimização do banco de dados"),
    ("settings.projection.manage", "Gerenciar projeção de lançamentos recorrentes"),
    ("settings.password_policy.manage", "Gerenciar política de senha e bloqueio de login"),
    ("settings.backup.run", "Executar backup do banco de dados"),
    ("settings.restore.run", "Restaurar backup do banco de dados"),
    ("settings.operations.audit", "Auditar operações bancárias"),
    ("tables.users.manage", "Gerenciar usuários"),
    ("permissions.manage", "Gerenciar permissões de usuários"),
)


def seed_permissions(apps, schema_editor):
    permission_model = apps.get_model("accounts", "AppPermission")
    for name, description in PERMISSIONS:
        permission_model.objects.update_or_create(name=name, defaults={"description": description})


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_initial")]

    operations = [migrations.RunPython(seed_permissions, migrations.RunPython.noop)]
