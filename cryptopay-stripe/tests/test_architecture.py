"""«Фитнес-функция» архитектуры: автоматическая проверка правила зависимостей.

* domain зависит только от себя и «чистой» стандартной библиотеки;
* application — только от domain и себя;
* mockwallet — имитация ВНЕШНЕЙ системы, поэтому не знает ничего о CryptoPay:
  ни домена, ни сервисов, ни инфраструктуры. С CryptoPay его связывает только
  адаптер MockWalletAdapter, который живёт в infrastructure.

Это SOLID (D), закреплённый тестом: нарушите правило — сборка упадёт.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "cryptopay"

# Белый список стандартной библиотеки: только «чистые» модули без ввода-вывода.
PURE_STDLIB = {
    "__future__",
    "abc",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "enum",
    "json",
    "re",
    "typing",
}


def python_files(layer: str) -> list[Path]:
    return sorted((PACKAGE / layer).rglob("*.py"))


def imports_of(file: Path) -> list[str]:
    """Абсолютные имена всех импортов файла (относительные переводятся в cryptopay.…)."""
    module_parts = file.relative_to(PACKAGE.parent).with_suffix("").parts
    package = list(module_parts[:-1])
    names: list[str] = []
    for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                names.append(".".join([*base, node.module] if node.module else base))
            elif node.module:
                names.append(node.module)
    return names


def top(name: str) -> str:
    return name.split(".")[0]


@pytest.mark.parametrize("file", python_files("domain"), ids=lambda f: f.name)
def test_domain_depends_only_on_itself(file: Path) -> None:
    for name in imports_of(file):
        assert name.startswith("cryptopay.domain") or top(name) in PURE_STDLIB, f"{file.name} импортирует {name}"


@pytest.mark.parametrize("file", python_files("application"), ids=lambda f: str(f.relative_to(PACKAGE)))
def test_application_depends_only_on_domain_and_itself(file: Path) -> None:
    for name in imports_of(file):
        allowed = name.startswith(("cryptopay.domain", "cryptopay.application")) or top(name) in PURE_STDLIB
        assert allowed, f"{file.relative_to(PACKAGE)} импортирует {name}"


@pytest.mark.parametrize("file", python_files("mockwallet"), ids=lambda f: f.name)
def test_mock_wallet_knows_nothing_about_cryptopay(file: Path) -> None:
    for name in imports_of(file):
        if top(name) == "cryptopay":
            assert name.startswith("cryptopay.mockwallet"), f"mockwallet/{file.name} импортирует {name}"


def test_inner_layers_do_not_import_io_libraries() -> None:
    forbidden = {
        *("fastapi", "starlette", "pydantic", "uvicorn"),  # веб-фреймворк
        *("psycopg", "psycopg_pool"),  # база данных
        *("jwt", "httpx"),  # прочие адаптеры
        *("os", "socket", "http", "urllib", "logging", "secrets", "hashlib"),  # ввод-вывод и криптография
    }
    for file in [*python_files("domain"), *python_files("application")]:
        used = {top(name) for name in imports_of(file)} & forbidden
        assert not used, f"{file.relative_to(PACKAGE)} импортирует {used}"


def test_only_the_adapter_and_composition_root_know_the_mock_wallet() -> None:
    allowed = {
        PACKAGE / "container.py",
        PACKAGE / "infrastructure" / "wallet" / "mock_wallet_adapter.py",
        PACKAGE / "infrastructure" / "http" / "app.py",  # подключает HTTP-страницы имитатора
    }
    for file in PACKAGE.rglob("*.py"):
        if file.is_relative_to(PACKAGE / "mockwallet") or file in allowed:
            continue
        users = [name for name in imports_of(file) if name.startswith("cryptopay.mockwallet")]
        assert not users, f"{file.relative_to(PACKAGE)} напрямую импортирует имитатор кошелька: {users}"
