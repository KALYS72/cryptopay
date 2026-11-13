"""Пути к файлам проекта. Корень — папка, в которой лежат cryptopay/, migrations/ и public/."""

from __future__ import annotations

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = APP_ROOT / "migrations"
PUBLIC_DIR = APP_ROOT / "public"
