"""Имя, импортированное внутри ветки, не должно использоваться вне её.

Класс дефектов, найденный широким прогоном по семи чертежам вместо одного.

`localize_turned_features` звала `pytesseract.image_to_data` безусловно, а
`import pytesseract` стоял только в двух условных ветках выше — для Python имя
локальное и незаполненное, когда те ветки не выполнились. На цветном чертеже
это не всплывало: функция раньше выходила по блокеру «геометрия не отделена по
цвету» и до места использования не доходила. Как только общая маска довела
выполнение туда, первый же реальный скан (detal_126.png) упал с
`UnboundLocalError`.

`cad_solid._cut_features` был испорчен иначе, но с тем же исходом: `import math`
внутри одного цикла ЗАТЕНЯЛ модульный импорт и делал `math` локальным, а
использовался он в другом цикле. Круговой массив отверстий без первого цикла
уронил бы сборку тела.

Оба дефекта тихие: код читается правильно, падает только на той комбинации
веток, которую никто не пробовал.
"""

from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app" / "ai"

_TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _shadowed_imports(path: pathlib.Path) -> list[str]:
    """Имена, которые видны не на всех путях до места использования."""
    problems: list[str] = []
    tree = ast.parse(path.read_text())
    for func in [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]:
        covered: dict[str, set] = {}
        lines: dict[str, set[int]] = {}
        for name, imp, guaranteed in _conditional_imports(func):
            covered.setdefault(name, set()).update(guaranteed)
            covered[name].add(imp)
            lines.setdefault(name, set()).add(imp.lineno)

        for name, reachable in covered.items():
            for use in _loads_of(func, name):
                if use in reachable:
                    continue
                where = ", ".join(str(line) for line in sorted(lines[name]))
                problems.append(
                    f"{path.name}:{func.name}: «{name}» импортируется только в ветках "
                    f"(строки {where}), а используется на строке {use.lineno}"
                )
    return problems


def _conditional_imports(func: ast.AST):
    """Импорты внутри веток и множество узлов, которым имя гарантированно видно.

    Гарантированно — то, что стоит ПОСЛЕ импорта в том же списке операторов, со
    всем вложенным. Соседняя ветка `else`, следующий `except` и код после блока
    такой гарантии сами по себе не дают: туда можно попасть, не выполнив импорт.

    Исключение — распространённая и правильная форма
    ``try: import x / except ImportError: return``: раз альтернативный путь
    выходит из функции, после блока имя связано. Тогда область расширяется на
    хвост родительского списка, и так вверх, пока правило выполняется.

    Импорт в теле самой функции пропускается: он виден везде.
    """
    parents = {child: node for node in ast.walk(func) for child in ast.iter_child_nodes(node)}
    top_level = {
        (alias.asname or alias.name).split(".")[0]
        for node in func.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    for block in ast.walk(func):
        for field, value in ast.iter_fields(block):
            if not isinstance(value, list):
                continue
            for index, stmt in enumerate(value):
                if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    continue
                if block is func and field == "body":
                    continue
                reachable = _guaranteed_region(value[index:], block, field, func, parents)
                for alias in stmt.names:
                    name = (alias.asname or alias.name).split(".")[0]
                    if name not in top_level:
                        yield name, stmt, reachable


def _guaranteed_region(tail, block, field, func, parents) -> set:
    """Хвост своего списка операторов плюс, если ветка «безальтернативна», хвост родителя."""
    region = {node for stmt in tail for node in ast.walk(stmt)}
    while block is not func and _alternatives_terminate(block, field):
        parent = parents.get(block)
        if parent is None:
            break
        siblings = _list_containing(parent, block)
        if siblings is None:
            break
        after = siblings[siblings.index(block) + 1 :]
        region.update(node for stmt in after for node in ast.walk(stmt))
        field = _field_of(parent, siblings)
        block = parent
    return region


def _alternatives_terminate(block, field: str) -> bool:
    """Все ли пути в обход этой ветки выходят до кода, что стоит после блока."""
    if isinstance(block, ast.Try) and field == "body":
        paths = [handler.body for handler in block.handlers]
        return bool(paths) and all(_terminates(path) for path in paths)
    if isinstance(block, ast.If):
        other = block.orelse if field == "body" else block.body
        return bool(other) and _terminates(other)
    return False


def _terminates(body: list) -> bool:
    return bool(body) and isinstance(body[-1], _TERMINATORS)


def _list_containing(parent, node):
    for _name, value in ast.iter_fields(parent):
        if isinstance(value, list) and node in value:
            return value
    return None


def _field_of(parent, target: list) -> str:
    for name, value in ast.iter_fields(parent):
        if value is target:
            return name
    return ""


def _loads_of(func: ast.AST, name: str):
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == name
    ]


def test_no_conditional_import_is_used_outside_its_branch():
    problems: list[str] = []
    for path in sorted(_ROOT.rglob("*.py")):
        problems.extend(_shadowed_imports(path))

    assert not problems, "имя доступно не на всех путях:\n" + "\n".join(problems)
