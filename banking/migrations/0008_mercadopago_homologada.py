from django.db import migrations


def set_mercadopago_homologada(apps, schema_editor):
    FinancialInstitution = apps.get_model('banking', 'FinancialInstitution')
    FinancialInstitution.objects.filter(institution_name__iexact='Mercado Pago').update(homologada=True)


def unset_mercadopago_homologada(apps, schema_editor):
    FinancialInstitution = apps.get_model('banking', 'FinancialInstitution')
    FinancialInstitution.objects.filter(institution_name__iexact='Mercado Pago').update(homologada=False)


class Migration(migrations.Migration):

    dependencies = [
        ('banking', '0007_conta_tipo_aplicacao'),
    ]

    operations = [
        migrations.RunPython(set_mercadopago_homologada, unset_mercadopago_homologada),
    ]
