from django.db import migrations

NOMES = ("Itaú", "Itau")


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
        ('banking', '0011_xp_investimentos_homologada'),
    ]

    operations = [
        migrations.RunPython(homologar, desfazer),
    ]
