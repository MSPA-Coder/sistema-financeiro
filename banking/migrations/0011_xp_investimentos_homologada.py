from django.db import migrations

# A conta de investimento da XP foi cadastrada como "SCP XP Investimestos" (sic).
# O adapter de PDF serve aos dois nomes e à grafia correta.
NOMES = ("XP Investimentos", "SCP XP Investimentos", "SCP XP Investimestos")


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
        ('banking', '0010_finalidade_da_conta'),
    ]

    operations = [
        migrations.RunPython(homologar, desfazer),
    ]
