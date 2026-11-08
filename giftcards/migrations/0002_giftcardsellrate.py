from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('giftcards', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='GiftCardSellRate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('brand', models.CharField(max_length=100)),
                ('logo_url', models.CharField(blank=True, max_length=300)),
                ('country', models.CharField(max_length=2)),
                ('card_type', models.CharField(choices=[('physical', 'Physical Card'), ('ecode', 'E-Code')], max_length=20)),
                ('min_usd', models.DecimalField(decimal_places=2, default=1, max_digits=10)),
                ('max_usd', models.DecimalField(decimal_places=2, default=1000, max_digits=10)),
                ('rate_ngn', models.DecimalField(decimal_places=2, max_digits=10)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['brand', 'country', 'card_type', 'min_usd'],
            },
        ),
    ]
