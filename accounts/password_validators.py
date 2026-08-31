"""Validador de senha com política configurável via AppSetting.

Tamanho mínimo configurável (com piso de 8 caracteres, que a configuração não
pode furar) mais requisitos opcionais de maiúsculas, números e especiais.

Ocupa o lugar de `MinimumLengthValidator` em AUTH_PASSWORD_VALIDATORS de
propósito: os dois juntos aplicariam mínimos diferentes, e a tela
Configurações > Parâmetros passaria a exibir um valor que não é o realmente
aplicado.
"""
from __future__ import annotations

import re

from django.core.exceptions import ValidationError

DEFAULT_MIN_LENGTH = 8
DEFAULT_MIN_UPPERCASE = 0
DEFAULT_MIN_NUMBERS = 0
DEFAULT_MIN_SPECIAL = 0
MIN_LENGTH_FLOOR = 8
MAX_LENGTH_CEILING = 256

_SPECIAL_CHARS_RE = re.compile(r"[^A-Za-z0-9]")
_UPPERCASE_RE = re.compile(r"[A-Z]")
_NUMBER_RE = re.compile(r"[0-9]")


def _current_settings():
    from core.domain.settings import (
        APP_SETTING_PASSWORD_MIN_LENGTH,
        APP_SETTING_PASSWORD_MIN_NUMBERS,
        APP_SETTING_PASSWORD_MIN_SPECIAL,
        APP_SETTING_PASSWORD_MIN_UPPERCASE,
    )
    from core.services import get_app_setting

    def _int_setting(key, default):
        try:
            return int(get_app_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    return {
        "min_length": max(_int_setting(APP_SETTING_PASSWORD_MIN_LENGTH, DEFAULT_MIN_LENGTH), MIN_LENGTH_FLOOR),
        "min_uppercase": max(_int_setting(APP_SETTING_PASSWORD_MIN_UPPERCASE, DEFAULT_MIN_UPPERCASE), 0),
        "min_numbers": max(_int_setting(APP_SETTING_PASSWORD_MIN_NUMBERS, DEFAULT_MIN_NUMBERS), 0),
        "min_special": max(_int_setting(APP_SETTING_PASSWORD_MIN_SPECIAL, DEFAULT_MIN_SPECIAL), 0),
    }


def current_min_length() -> int:
    """Tamanho mínimo de senha em vigor, já com o piso aplicado.

    Público porque a redefinição de senha precisa dele: o sorteio da senha
    temporária tem de produzir algo que esta mesma política aceite. Sem isso, a
    instalação que configurar 20 caracteres teria a redefinição recusando a
    própria senha que acabou de sortear -- foi o que aconteceu no banco local,
    configurado em 15 contra um sorteio de 12.
    """
    try:
        return int(_current_settings()["min_length"])
    except Exception:
        return DEFAULT_MIN_LENGTH


class ConfigurablePasswordPolicyValidator:
    """Substitui MinimumLengthValidator: lê a política de app_setting em tempo real."""

    def validate(self, password, user=None):
        try:
            policy = _current_settings()
        except Exception:
            policy = {
                "min_length": DEFAULT_MIN_LENGTH,
                "min_uppercase": DEFAULT_MIN_UPPERCASE,
                "min_numbers": DEFAULT_MIN_NUMBERS,
                "min_special": DEFAULT_MIN_SPECIAL,
            }

        if len(password) < policy["min_length"]:
            raise ValidationError(
                f"A senha deve ter pelo menos {policy['min_length']} caracteres.",
                code="password_too_short",
            )
        if len(password) > MAX_LENGTH_CEILING:
            raise ValidationError(
                f"A senha deve ter no máximo {MAX_LENGTH_CEILING} caracteres.",
                code="password_too_long",
            )
        if policy["min_uppercase"] and len(_UPPERCASE_RE.findall(password)) < policy["min_uppercase"]:
            raise ValidationError(
                f"A senha deve conter pelo menos {policy['min_uppercase']} letra(s) maiúscula(s).",
                code="password_missing_uppercase",
            )
        if policy["min_numbers"] and len(_NUMBER_RE.findall(password)) < policy["min_numbers"]:
            raise ValidationError(
                f"A senha deve conter pelo menos {policy['min_numbers']} número(s).",
                code="password_missing_number",
            )
        if policy["min_special"] and len(_SPECIAL_CHARS_RE.findall(password)) < policy["min_special"]:
            raise ValidationError(
                f"A senha deve conter pelo menos {policy['min_special']} caractere(s) especial(is).",
                code="password_missing_special",
            )

    def get_help_text(self):
        try:
            policy = _current_settings()
        except Exception:
            return f"Sua senha deve ter pelo menos {DEFAULT_MIN_LENGTH} caracteres."
        parts = [f"pelo menos {policy['min_length']} caracteres"]
        if policy["min_uppercase"]:
            parts.append(f"{policy['min_uppercase']} maiúscula(s)")
        if policy["min_numbers"]:
            parts.append(f"{policy['min_numbers']} número(s)")
        if policy["min_special"]:
            parts.append(f"{policy['min_special']} caractere(s) especial(is)")
        return "Sua senha deve conter " + ", ".join(parts) + "."
