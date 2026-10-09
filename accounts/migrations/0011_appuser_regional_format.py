# Formato regional (Brasil/EUA) de datas e números, escolhido por usuário.
#
# Só apresentação: nada que já está gravado muda. Todo usuário existente fica
# em "br", o formato que o sistema sempre mostrou. Reverter descarta a escolha.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_remover_contas_em_analises'),
    ]

    operations = [
        migrations.AddField(
            model_name='appuser',
            name='regional_format',
            field=models.CharField(
                choices=[('br', 'br'), ('us', 'us')],
                default='br',
                max_length=2,
            ),
        ),
        migrations.AddConstraint(
            model_name='appuser',
            constraint=models.CheckConstraint(
                condition=models.Q(('regional_format__in', ('br', 'us'))),
                name='ck_app_user_regional_format_valid',
            ),
        ),
    ]
