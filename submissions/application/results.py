from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandResult:
    message: str
    level: str = "success"
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RenderResult:
    template: str
    context: dict = field(default_factory=dict)

