"""Chaves e valores permitidos para configurações da aplicação."""
from __future__ import annotations

from typing import Final

# Tema e densidade de tabela sao preferencias por usuario e ficam em
# `AppUser.ui_theme` / `AppUser.table_scroll_rows`, nao em app_setting.
APP_SETTING_LAST_OPTIMIZE_INFO: Final = "last_optimize_info"
APP_SETTING_PROJECTION_HORIZON_MONTHS: Final = "projection_horizon_months"
APP_SETTING_PROJECTION_RUN_DAY: Final = "projection_run_day"
APP_SETTING_LAST_PROJECTION_RUN: Final = "last_projection_run"
APP_SETTING_SYSTEM_START_DATE: Final = "system_start_date"
APP_SETTING_PASSWORD_MIN_LENGTH: Final = "password_min_length"
APP_SETTING_PASSWORD_MIN_UPPERCASE: Final = "password_min_uppercase"
APP_SETTING_PASSWORD_MIN_NUMBERS: Final = "password_min_numbers"
APP_SETTING_PASSWORD_MIN_SPECIAL: Final = "password_min_special"
APP_SETTING_LOGIN_LOCK_MAX_FAILURES: Final = "login_lock_max_failures"
APP_SETTING_LOGIN_LOCK_MINUTES: Final = "login_lock_minutes"

UI_THEME_LIGHT: Final = "light"
UI_THEME_DARK: Final = "dark"
UI_THEME_SOLARIZED_LIGHT: Final = "solarized_light"
UI_THEME_SOLARIZED_DARK: Final = "solarized_dark"
UI_THEME_DRACULA: Final = "dracula"
UI_THEME_NORD: Final = "nord"
UI_THEME_MONOKAI: Final = "monokai"
UI_THEME_GRAY: Final = "gray"
UI_THEME_SOFT_LIGHT: Final = "soft_light"
UI_THEME_SOFT_DARK: Final = "soft_dark"
UI_THEME_CORPORATE_BLUE: Final = "corporate_blue"
UI_THEME_EMERALD: Final = "emerald"
VALID_UI_THEMES: Final = (
    UI_THEME_LIGHT,
    UI_THEME_DARK,
    UI_THEME_SOLARIZED_LIGHT,
    UI_THEME_SOLARIZED_DARK,
    UI_THEME_DRACULA,
    UI_THEME_NORD,
    UI_THEME_MONOKAI,
    UI_THEME_GRAY,
    UI_THEME_SOFT_LIGHT,
    UI_THEME_SOFT_DARK,
    UI_THEME_CORPORATE_BLUE,
    UI_THEME_EMERALD,
)
UI_THEME_LABELS: Final = {
    UI_THEME_LIGHT: "Light",
    UI_THEME_DARK: "Dark",
    UI_THEME_SOLARIZED_LIGHT: "Solarized Light",
    UI_THEME_SOLARIZED_DARK: "Solarized Dark",
    UI_THEME_DRACULA: "Dracula",
    UI_THEME_NORD: "Nord",
    UI_THEME_MONOKAI: "Monokai",
    UI_THEME_GRAY: "Gray Scale",
    UI_THEME_SOFT_LIGHT: "Soft Light",
    UI_THEME_SOFT_DARK: "Soft Dark",
    UI_THEME_CORPORATE_BLUE: "Corporate Blue",
    UI_THEME_EMERALD: "Emerald",
}
UI_THEME_DESCRIPTIONS: Final = {
    UI_THEME_LIGHT: "Claro e neutro",
    UI_THEME_DARK: "Escuro e focado",
    UI_THEME_SOLARIZED_LIGHT: "Claro suave",
    UI_THEME_SOLARIZED_DARK: "Escuro suave",
    UI_THEME_DRACULA: "Alto contraste",
    UI_THEME_NORD: "Azul frio",
    UI_THEME_MONOKAI: "Técnico vibrante",
    UI_THEME_GRAY: "Neutro analítico",
    UI_THEME_SOFT_LIGHT: "Claro confortável",
    UI_THEME_SOFT_DARK: "Escuro confortável",
    UI_THEME_CORPORATE_BLUE: "Azul corporativo",
    UI_THEME_EMERALD: "Verde financeiro",
}


def normalize_ui_theme(value: str | None) -> str:
    return value if value in VALID_UI_THEMES else UI_THEME_LIGHT


TABLE_SCROLL_ROWS_MIN: Final = 5
TABLE_SCROLL_ROWS_MAX: Final = 200


def normalize_table_scroll_rows(value: object, default: int = 15) -> int:
    if not isinstance(value, (int, float, str)):
        return default
    try:
        rows = int(value)
    except (TypeError, ValueError):
        return default
    return max(TABLE_SCROLL_ROWS_MIN, min(rows, TABLE_SCROLL_ROWS_MAX))
