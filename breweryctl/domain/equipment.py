"""糖化间设备占用登记：糖化锅与煮沸锅同一时刻只能服务一个批次。

与发酵罐不同，糖化锅、煮沸锅在领域模型里没有自己的状态机，
它们是否空闲完全由本模块的占用记录（``batch_id`` 字段）表达。
占用与释放都在 ``equipment:{brewery_id}:{kind}`` 锁内完成，
保证两个批次不会同时拿到同一口锅。
"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.errors import ConflictError
from ..core.ids import new_id, vessel_code
from ..core.validators import require_choice, require_text
from ..persistence.store import FileStore, merge_documents
from .models import BrewhouseVessel

EQUIPMENT = "equipment"

VESSEL_KINDS = ("mash_tun", "boil_kettle")

KIND_LABELS = {"mash_tun": "糖化锅", "boil_kettle": "煮沸锅"}


class EquipmentRegistry:
    """登记糖化间容器，并以原子方式占用与释放。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.vessels = store.collection(EQUIPMENT)

    def register_vessel(
        self,
        brewery_id: str,
        kind: str,
        line_id: str | None = None,
    ) -> dict[str, Any]:
        """为工厂登记一口糖化锅或煮沸锅。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_kind = require_choice(kind, field="kind", choices=VESSEL_KINDS)
        with self.store.locks.guard(f"equipment:{clean_brewery}:{clean_kind}"):
            index = len(
                [
                    item
                    for item in self.vessels.all()
                    if item.get("brewery_id") == clean_brewery and item.get("kind") == clean_kind
                ]
            ) + 1
            now = format_moment(self.clock.now())
            vessel = BrewhouseVessel(
                id=new_id("vessel"),
                code=vessel_code(clean_kind, index),
                kind=clean_kind,
                brewery_id=clean_brewery,
                line_id=line_id,
                updated_at=now,
            )
            return self.vessels.put(vessel.id, vessel.to_doc())

    def acquire(self, brewery_id: str, kind: str, batch_id: str) -> dict[str, Any]:
        """把一口空闲容器分配给批次；同批次重复获取是幂等的。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_kind = require_choice(kind, field="kind", choices=VESSEL_KINDS)
        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        with self.store.locks.guard(f"equipment:{clean_brewery}:{clean_kind}"):
            held = self.vessel_of(clean_batch, clean_kind)
            if held is not None:
                return held
            free = sorted(
                (
                    item
                    for item in self.vessels.all()
                    if item.get("brewery_id") == clean_brewery
                    and item.get("kind") == clean_kind
                    and not item.get("batch_id")
                ),
                key=lambda item: str(item.get("code", "")),
            )
            if not free:
                raise ConflictError(
                    f"没有空闲的{KIND_LABELS[clean_kind]}，批次需要等待",
                    brewery_id=clean_brewery,
                    kind=clean_kind,
                    batch_id=clean_batch,
                )
            vessel = free[0]
            now = format_moment(self.clock.now())

            def mutate(document: dict[str, Any]) -> dict[str, Any]:
                if document.get("batch_id"):
                    raise ConflictError(
                        f"{KIND_LABELS[clean_kind]}已被其他批次占用",
                        vessel_id=document.get("id"),
                        batch_id=document.get("batch_id"),
                    )
                return merge_documents(
                    document,
                    [
                        ("batch_id", clean_batch),
                        ("acquired_at", now),
                        ("updated_at", now),
                    ],
                )

            return self.vessels.update(str(vessel["id"]), mutate)

    def release_batch(self, batch_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        """释放批次占用的容器；幂等，未占用时返回空列表。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        released: list[dict[str, Any]] = []
        now = format_moment(self.clock.now())
        for vessel in self.vessels.all():
            if vessel.get("batch_id") != clean_batch:
                continue
            if kind and vessel.get("kind") != kind:
                continue

            def mutate(document: dict[str, Any]) -> dict[str, Any]:
                return merge_documents(
                    document,
                    [("batch_id", None), ("acquired_at", None), ("updated_at", now)],
                )

            released.append(self.vessels.update(str(vessel["id"]), mutate))
        return released

    def vessel_of(self, batch_id: str, kind: str) -> dict[str, Any] | None:
        """返回批次占用的指定类型容器。"""

        for item in self.vessels.all():
            if item.get("batch_id") == batch_id and item.get("kind") == kind:
                return item
        return None

    def vessels_for(self, brewery_id: str) -> list[dict[str, Any]]:
        """返回工厂下的全部容器。"""

        return sorted(
            (item for item in self.vessels.all() if item.get("brewery_id") == brewery_id),
            key=lambda item: (str(item.get("kind", "")), str(item.get("code", ""))),
        )

    def summary(self) -> dict[str, Any]:
        """汇总容器数量与占用情况。"""

        items = self.vessels.all()
        by_kind: dict[str, dict[str, int]] = {}
        for item in items:
            bucket = by_kind.setdefault(str(item.get("kind")), {"total": 0, "occupied": 0})
            bucket["total"] += 1
            if item.get("batch_id"):
                bucket["occupied"] += 1
        return {
            "vessels": len(items),
            "by_kind": by_kind,
            "occupied": len([item for item in items if item.get("batch_id")]),
        }
