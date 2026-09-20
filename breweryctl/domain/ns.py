"""酿造命名空间：工厂、产线、在制配额与热端容器位。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import NotFoundError, QuotaExceededError, ValidationError
from ..core.ids import new_id, slugify
from ..core.validators import require_int, require_number, require_text
from ..persistence.store import FileStore

BREWERIES = "breweries"
LINES = "lines"


class NamespaceRegistry:
    """维护工厂与产线，并限制同时在制批次数。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.breweries = store.collection(BREWERIES)
        self.lines = store.collection(LINES)

    def register_brewery(self, name: str, code: str, location: str = "") -> dict[str, Any]:
        """登记一个酿造工厂。"""

        clean_name = require_text(name, field="name", max_length=80)
        clean_code = slugify(require_text(code, field="code", max_length=32))
        if any(item.get("code") == clean_code for item in self.breweries.all()):
            raise ValidationError("工厂编码已存在", code=clean_code)
        now = format_moment(self.clock.now())
        document = {
            "id": new_id("brew"),
            "name": clean_name,
            "code": clean_code,
            "location": require_text(location, field="location", max_length=120) if location else "",
            "active_batches": [],
            "created_at": now,
            "updated_at": now,
        }
        return self.breweries.put(document["id"], document)

    def list_breweries(self) -> list[dict[str, Any]]:
        """返回全部工厂。"""

        return sorted(self.breweries.all(), key=lambda item: item.get("code", ""))

    def get_brewery(self, brewery_id: str) -> dict[str, Any]:
        """按标识读取工厂。"""

        return self.breweries.require(brewery_id, label="工厂")

    def add_line(
        self,
        brewery_id: str,
        name: str,
        capacity_hl: float,
        vessel_count: int = 1,
    ) -> dict[str, Any]:
        """为工厂增加一条产线。"""

        brewery = self.get_brewery(brewery_id)
        clean_name = require_text(name, field="name", max_length=80)
        capacity = require_number(capacity_hl, field="capacity_hl", minimum=1.0, maximum=10_000.0)
        vessels = require_int(vessel_count, field="vessel_count", minimum=1, maximum=64)
        now = format_moment(self.clock.now())
        document = {
            "id": new_id("line"),
            "brewery_id": brewery["id"],
            "name": clean_name,
            "capacity_hl": capacity,
            "vessel_count": vessels,
            "active_batches": [],
            "created_at": now,
            "updated_at": now,
        }
        return self.lines.put(document["id"], document)

    def lines_for(self, brewery_id: str) -> list[dict[str, Any]]:
        """返回工厂下的产线。"""

        self.get_brewery(brewery_id)
        return self.lines.find(lambda item: item.get("brewery_id") == brewery_id)

    def reserve_slot(self, brewery_id: str, batch_id: str) -> dict[str, Any]:
        """占用一个在制批次名额，超出配额时拒绝。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            active = list(document.get("active_batches", []))
            if clean_batch in active:
                return document
            if len(active) >= self.settings.max_active_batches:
                raise QuotaExceededError(
                    "工厂在制批次已达配额",
                    brewery_id=brewery_id,
                    limit=self.settings.max_active_batches,
                    active=active,
                )
            active.append(clean_batch)
            document["active_batches"] = active
            document["updated_at"] = format_moment(self.clock.now())
            return document

        self.breweries.update(brewery_id, mutate)
        return self.usage(brewery_id)

    def release_slot(self, brewery_id: str, batch_id: str) -> dict[str, Any]:
        """释放在制批次名额。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            active = [item for item in document.get("active_batches", []) if item != clean_batch]
            document["active_batches"] = active
            document["updated_at"] = format_moment(self.clock.now())
            return document

        self.breweries.update(brewery_id, mutate)
        return self.usage(brewery_id)

    def usage(self, brewery_id: str) -> dict[str, Any]:
        """返回配额使用情况。"""

        brewery = self.get_brewery(brewery_id)
        active = list(brewery.get("active_batches", []))
        limit = self.settings.max_active_batches
        return {
            "brewery_id": brewery["id"],
            "active": len(active),
            "limit": limit,
            "available": max(limit - len(active), 0),
            "batches": active,
        }

    def reserve_vessel(
        self,
        brewery_id: str,
        batch_id: str,
        line_id: str | None = None,
    ) -> dict[str, Any]:
        """为批次占用一个热端容器位（糖化锅/煮沸锅），全部占用时拒绝。

        容器位是全局互斥资源：一个批次从创建（进入热端）一直持有到
        转出热端（转罐、中止或崩溃恢复），期间其他批次不能使用同一容器位。
        """

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        lines = self.lines_for(brewery_id)
        if line_id is not None:
            clean_line = require_text(line_id, field="line_id", max_length=64)
            lines = [item for item in lines if item.get("id") == clean_line]
            if not lines:
                raise NotFoundError("产线不存在", brewery_id=brewery_id, line_id=clean_line)
        if not lines:
            raise NotFoundError("工厂没有可用产线", brewery_id=brewery_id)
        for line in lines:
            if self._try_reserve_vessel(str(line["id"]), clean_batch):
                return self.lines.require(str(line["id"]), label="产线")
        raise QuotaExceededError(
            "糖化线热端容器位已满，无法并行新批次",
            brewery_id=brewery_id,
            lines=self.vessel_usage(brewery_id)["lines"],
        )

    def release_vessel(self, brewery_id: str, batch_id: str) -> None:
        """释放批次占用的热端容器位；幂等，未占用时不报错。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        for line in self.lines_for(brewery_id):

            def mutate(document: dict[str, Any]) -> dict[str, Any]:
                active = [
                    item for item in document.get("active_batches", []) if item != clean_batch
                ]
                if len(active) != len(document.get("active_batches", [])):
                    document["active_batches"] = active
                    document["updated_at"] = format_moment(self.clock.now())
                return document

            self.lines.update(str(line["id"]), mutate)

    def vessel_usage(self, brewery_id: str) -> dict[str, Any]:
        """返回每条产线的热端容器位占用情况。"""

        lines: list[dict[str, Any]] = []
        for line in self.lines_for(brewery_id):
            active = list(line.get("active_batches", []))
            capacity = int(line.get("vessel_count", 1))
            lines.append(
                {
                    "line_id": line["id"],
                    "name": line.get("name"),
                    "capacity": capacity,
                    "active": len(active),
                    "available": max(capacity - len(active), 0),
                    "batches": active,
                }
            )
        return {"brewery_id": brewery_id, "lines": lines}

    def describe(self) -> dict[str, Any]:
        """汇总命名空间概况。"""

        breweries = self.list_breweries()
        return {
            "breweries": len(breweries),
            "lines": self.lines.count(),
            "quota": {
                item["id"]: self.usage(item["id"])["active"] for item in breweries
            },
            "vessels": {
                item["id"]: self.vessel_usage(item["id"])["lines"] for item in breweries
            },
        }

    def seed_default(self) -> dict[str, Any]:
        """在没有工厂时建立一个可运行的默认命名空间。"""

        breweries = self.list_breweries()
        if breweries:
            return breweries[0]
        brewery = self.register_brewery("示范酿造厂", "demo", "上海")
        self.add_line(brewery["id"], "一号糖化线", 120.0, 2)
        return brewery

    def _try_reserve_vessel(self, line_id: str, batch_id: str) -> bool:
        """在单条产线上尝试占用容器位，成功返回 ``True``。"""

        acquired = False

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            nonlocal acquired
            active = list(document.get("active_batches", []))
            if batch_id in active:
                acquired = True
                return document
            capacity = int(document.get("vessel_count", 1))
            if len(active) >= capacity:
                return document
            active.append(batch_id)
            document["active_batches"] = active
            document["updated_at"] = format_moment(self.clock.now())
            acquired = True
            return document

        self.lines.update(line_id, mutate)
        return acquired
