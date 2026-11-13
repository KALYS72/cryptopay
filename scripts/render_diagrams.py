"""Рендер всех диаграмм docs/diagrams/*.puml в PNG и SVG.

Нужны Java 11+ и plantuml.jar (https://plantuml.com/download):

    PLANTUML_JAR=/путь/к/plantuml.jar python scripts/render_diagrams.py

Готовые картинки уже лежат в docs/diagrams/png и docs/diagrams/svg.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

DIAGRAMS = Path(__file__).resolve().parent.parent / "docs" / "diagrams"


def main() -> None:
    jar = os.environ.get("PLANTUML_JAR", "")
    if not jar or not Path(jar).is_file():
        sys.exit("Укажите путь к plantuml.jar: PLANTUML_JAR=/path/plantuml.jar python scripts/render_diagrams.py")
    sources = sorted(p.name for p in DIAGRAMS.glob("*.puml"))
    for fmt in ("png", "svg"):
        out = DIAGRAMS / fmt
        out.mkdir(exist_ok=True)
        subprocess.run(
            [
                "java",
                "-Djava.awt.headless=true",
                "-jar",
                jar,
                f"-t{fmt}",
                "-charset",
                "UTF-8",
                "-o",
                str(out),
                *sources,
            ],
            cwd=DIAGRAMS,
            check=True,
        )
        # PlantUML называет файлы по @startuml <имя>; переименуем по имени исходника, чтобы сохранить нумерацию.
        for source in sources:
            plain = re.sub(r"^\d+-", "", source.removesuffix(".puml"))
            produced = out / f"{plain}.{fmt}"
            if produced.exists():
                produced.replace(out / source.replace(".puml", f".{fmt}"))
    print(f"Готово: {len(sources)} диаграмм → docs/diagrams/png и docs/diagrams/svg")


if __name__ == "__main__":
    main()
