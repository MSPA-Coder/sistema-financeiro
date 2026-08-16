from django.db import migrations, models


def set_genial_homologada(apps, schema_editor):
    FinancialInstitution = apps.get_model('banking', 'FinancialInstitution')
    FinancialInstitution.objects.filter(institution_name__iexact='Genial').update(homologada=True)


def unset_genial_homologada(apps, schema_editor):
    FinancialInstitution = apps.get_model('banking', 'FinancialInstitution')
    FinancialInstitution.objects.filter(institution_name__iexact='Genial').update(homologada=False)


class Migration(migrations.Migration):

    dependencies = [
        ('banking', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='financialinstitution',
            name='homologada',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Corretora homologada para importação de extrato em PDF na rotina '
                    'de Bancos > Importações. Fora do CRUD: só é alterado via migração/shell.'
                ),
            ),
        ),
        migrations.RunPython(set_genial_homologada, unset_genial_homologada),
    ]
