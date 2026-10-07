from django.db import migrations

NOMES = ("Avenue",)


def homologar(apps, schema_editor):
    FinancialInstitution = apps.get_model('banking', 'FinancialInstitution')
    for nome in NOMES:
        FinancialInstitution.objects.filter(institution_name__iexact=nome).update(homologada=True)


def desfazer(apps, schema_editor):
    FinancialInstitution = apps.get_model('banking', 'FinancialInstitution')
    for nome in NOMES:
        FinancialInstitution.objects.filter(institution_name__iexact=nome).update(homologada=False)


class Migration(migrations.Migration):

    dependencies = [
        ('banking', '0013_conta_de_movimento_da_aplicacao'),
    ]

    operations = [
        migrations.RunPython(homologar, desfazer),
    ]
