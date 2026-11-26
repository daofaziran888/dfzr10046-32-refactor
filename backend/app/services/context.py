"""资源上下文：一次动作请求在「哪个模块、对哪条记录、以什么资格」上的统一描述。

- ``ModuleRef``：模块归属的稳定标识，用来区分同号记录属于哪个模块；
- ``ResourceContext``：模块 + 记录 + 发起方（前端工作台/内部调用）+ 可选写入资格。

动作分派只认这个上下文，不再从裸 ``entry_id`` 反推模块，避免同号记录被串到别的模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModuleRef:
    """模块引用：模块 key 与实体名的组合，作为跨模块动作取回时的归属凭据。"""

    key: str
    entity: str = ""

    def matches(self, other: "ModuleRef") -> bool:
        return self.key == other.key

    def to_dict(self) -> dict[str, str]:
        return {"module": self.key, "entity": self.entity}


@dataclass(frozen=True)
class ResourceContext:
    """资源上下文：动作分派的目标资源及其写入资格。

    ``origin`` 记录发起动作的模块归属（工作台页面/内部编排），回包按它接回原模块；
    ``token`` 是可选的在途请求认领凭据，未传时由服务端在动作解析阶段补发。
    """

    module: ModuleRef
    entry_id: int
    origin: ModuleRef | None = None
    token: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, module_key: str, entity: str, entry_id: int,
           origin_key: str | None = None, token: str | None = None,
           **extra: Any) -> "ResourceContext":
        return cls(
            module=ModuleRef(module_key, entity),
            entry_id=entry_id,
            origin=ModuleRef(origin_key, "") if origin_key else None,
            token=token,
            extra=dict(extra),
        )

    def belongs_to(self, module_key: str) -> bool:
        """按发起时的模块归属校验：回包/写入只能回到这个模块。"""
        return self.module.key == module_key

    def same_target(self, other: "ResourceContext") -> bool:
        """同一目标资源：模块 key 与记录号都一致才算同一条。"""
        return self.module.key == other.module.key and self.entry_id == other.entry_id
