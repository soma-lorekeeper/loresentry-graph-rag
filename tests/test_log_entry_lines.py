"""레이어 모듈의 공개 함수가 진입 줄을 남기는지 소스로 확인한다(LOG_SCHEMA.md 5절 "무엇을 남기나").

레이어 모듈은 LOG_LAYER 상수 또는 모듈 이름 끝 규칙(app/logs/_logger.py와 같다)이 controller·service·client·
repository·consumer·publisher·scheduler인 모듈이다. 그 모듈의 공개 함수와 공개 메서드(밑줄로 시작하지 않고,
property·dunder가 아닌 것)는 자기 본문에서 `AppLogger.current().info|warn|error(...)`를 부르거나
`LogFlow.schedule|kafka_record|task(...)`를 연다. 예외는 `@skip_entry_log("이유")`를 붙인다. Protocol(포트 선언)은
구현이 없어 보지 않는다.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
LAYERS = {
    "controller",
    "service",
    "client",
    "repository",
    "consumer",
    "publisher",
    "scheduler",
}
# app/logs/_logger.py의 모듈 이름 끝 규칙과 같다
NAME_RULE = {
    "routes": "controller",
    "service": "service",
    "services": "service",
    "store": "repository",
    "repository": "repository",
    "client": "client",
    "content": "client",
    "model": "client",
    "llm": "client",
    "consumer": "consumer",
    "handlers": "consumer",
    "publisher": "publisher",
    "worker": "scheduler",
    "relay": "scheduler",
    "outbox": "scheduler",
}
LOG_METHODS = {"info", "warn", "error"}
FLOW_METHODS = {"schedule", "kafka_record", "task"}


def layer(path: Path, tree: ast.Module) -> str:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "LOG_LAYER"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    name = path.stem
    if name in NAME_RULE:
        return NAME_RULE[name]
    return "client" if name.endswith("_client") else "other"


def decorator_names(function: ast.FunctionDef) -> set[str]:
    names = set()
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def own_nodes(function: ast.FunctionDef):
    """함수 자신의 본문 노드. 안쪽 함수·lambda·클래스의 본문은 그 함수의 것이라 건너뛴다."""
    stack = list(function.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(
                child,
                ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef,
            ):
                stack.append(child)


def logs_entry(function: ast.FunctionDef) -> bool:
    for node in own_nodes(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        # AppLogger.current().info(...)
        if (
            node.func.attr in LOG_METHODS
            and isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Attribute)
            and receiver.func.attr == "current"
            and isinstance(receiver.func.value, ast.Name)
            and receiver.func.value.id == "AppLogger"
        ):
            return True
        # LogFlow.task(...) 등
        if (
            node.func.attr in FLOW_METHODS
            and isinstance(receiver, ast.Name)
            and receiver.id == "LogFlow"
        ):
            return True
    return False


def public(function: ast.FunctionDef) -> bool:
    if function.name.startswith("_"):
        return False
    decorators = decorator_names(function)
    return not ({"property", "setter", "skip_entry_log"} & decorators)


def protocol(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Protocol")
        or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
        for base in node.bases
    )


def missing_entry_lines() -> list[str]:
    missing = []
    for path in sorted(APP.rglob("*.py")):
        if "logs" in path.relative_to(APP).parts[:1]:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if layer(path, tree) not in LAYERS:
            continue
        functions = [
            (None, node)
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not protocol(node):
                functions.extend(
                    (node.name, item)
                    for item in node.body
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                )
        for owner, function in functions:
            if public(function) and not logs_entry(function):
                where = path.relative_to(APP.parent)
                name = function.name if owner is None else f"{owner}.{function.name}"
                missing.append(f"{where}:{function.lineno} {name}")
    return missing


def test_layer_functions_log_an_entry_line_or_say_why_not():
    assert missing_entry_lines() == []


def test_the_check_sees_layer_modules():
    """검사가 비어서 통과하지 않는다: 이 저장소의 레이어 모듈을 실제로 본다."""
    layered = [
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if not path.relative_to(APP).as_posix().startswith("logs/")
        and layer(path, ast.parse(path.read_text(encoding="utf-8"))) in LAYERS
    ]
    for expected in (
        "main.py",
        "kafka/consumer.py",
        "kafka/handlers.py",
        "kafka/publisher.py",
        "refresh/service.py",
        "refresh/outbox.py",
        "adapters/refresh_inbox.py",
        "adapters/refresh_artifacts.py",
        "adapters/neptune_client.py",
    ):
        assert expected in layered
