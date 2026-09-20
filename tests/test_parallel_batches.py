"""多批次并行：批次私有状态隔离、全局资源互斥与故障回滚。"""

from __future__ import annotations

import threading
import unittest

from breweryctl.core.errors import ConflictError, QuotaExceededError, ValidationError

from .helpers import (
    StepClock,
    boil_to_cooling,
    create_batch,
    first_tank,
    make_app,
    mash_to_filter,
    published_recipe,
    sanitize_tank,
)


class ParallelIsolationTest(unittest.TestCase):
    """两个批次并行在制，任何操作不得影响另一批次。"""

    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.brewing = self.app.registry.brewing

    def tearDown(self) -> None:
        self.app.close()

    def test_two_batches_progress_independently(self) -> None:
        first = create_batch(self.app)
        second = create_batch(self.app)
        self.assertNotEqual(
            self.brewing.status(first)["batch"]["code"],
            self.brewing.status(second)["batch"]["code"],
        )

        self.brewing.confirm_water(first, 52.0, "probe-mash", "tester")
        self.brewing.charge_mash(first, 220.0, "tester")
        # 批次 B 未投料，状态不受批次 A 推进影响
        self.assertEqual("awaiting_water", self.brewing.status(second)["mash"]["stage"])

        self.brewing.confirm_water(second, 52.0, "probe-mash", "tester")
        self.brewing.charge_mash(second, 240.0, "tester")
        self.brewing.heat_mash(first, 65.0, "tester")
        self.brewing.rest_mash(first, 65.0, "tester")
        self.brewing.filter_mash(first, 5.2, 1020.0, 5.4, "tester")
        # 批次 B 仍停在投料完成，麦汁记录也未被 A 写入
        self.assertEqual("charged", self.brewing.status(second)["mash"]["stage"])
        self.assertEqual(0.0, self.brewing.status(second)["wort"]["gravity_plato"])

        self.brewing.heat_mash(second, 65.0, "tester")
        self.brewing.rest_mash(second, 65.0, "tester")
        self.brewing.filter_mash(second, 5.4, 1020.0, 5.4, "tester")
        self.assertEqual(5.2, self.brewing.status(first)["wort"]["gravity_plato"])
        self.assertEqual(5.4, self.brewing.status(second)["wort"]["gravity_plato"])

        self.brewing.ignite_boil(first, "tester")
        self.brewing.boil_rolling(first, "tester")
        self.brewing.add_hop(first, 1, 1.0, "tester")
        # A 的酒花投加不改变 B 的计划
        hops_second = self.brewing.status(second)["hops"]
        self.assertEqual(0, hops_second["added"])
        self.assertEqual(3, hops_second["pending"])

        boil_to_cooling(self.app, second)
        # A 还在煮沸早期，B 已进入降温，两条状态机互不影响
        self.assertEqual("boiling", self.brewing.status(first)["batch"]["stage"])
        self.assertEqual("cooling", self.brewing.status(second)["batch"]["stage"])

        self.brewing.cool_down(second, 9.0, "tester")
        self.assertEqual(9.0, self.brewing.status(second)["temperature"]["target_c"])
        self.assertNotIn("temperature", self.brewing.status(first))

    def test_abort_one_batch_keeps_the_other_running(self) -> None:
        first = create_batch(self.app)
        second = create_batch(self.app)
        self.brewing.abort_batch(first, "原料污染", "tester")
        view = self.brewing.status(second)
        self.assertEqual("mashing", view["batch"]["stage"])
        self.assertEqual("awaiting_water", view["mash"]["stage"])
        # 中止 A 释放了它的容器位，配额只统计 B
        brewery_id = view["batch"]["brewery_id"]
        self.assertEqual([second], self.app.registry.namespaces.usage(brewery_id)["batches"])


class VesselExclusionTest(unittest.TestCase):
    """热端容器位是全局互斥资源。"""

    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.brewing = self.app.registry.brewing

    def tearDown(self) -> None:
        self.app.close()

    def test_vessel_slots_limit_parallel_hot_side(self) -> None:
        # 种子产线 vessel_count=2，允许两个批次同时在热端
        create_batch(self.app)
        create_batch(self.app)
        with self.assertRaises(QuotaExceededError):
            create_batch(self.app)

    def test_transfer_releases_vessel_for_next_batch(self) -> None:
        first = create_batch(self.app)
        create_batch(self.app)
        with self.assertRaises(QuotaExceededError):
            create_batch(self.app)

        mash_to_filter(self.app, first)
        boil_to_cooling(self.app, first)
        self.brewing.mark_cooled(first, 10.0, "tester")
        tank_id = first_tank(self.app)
        sanitize_tank(self.app, tank_id)
        self.brewing.transfer_to_tank(first, tank_id, "tester")

        # 批次一转出热端后，新批次可以进入
        third = create_batch(self.app)
        self.assertEqual("mashing", self.brewing.status(third)["batch"]["stage"])

    def test_abort_releases_vessel(self) -> None:
        first = create_batch(self.app)
        create_batch(self.app)
        self.brewing.abort_batch(first, "换产", "tester")
        third = create_batch(self.app)
        self.assertEqual("mashing", self.brewing.status(third)["batch"]["stage"])

    def test_concurrent_create_allocates_unique_codes(self) -> None:
        app = make_app(clock=StepClock(), max_active_batches=8)
        try:
            brewery_id = str(app.registry.namespaces.list_breweries()[0]["id"])
            app.registry.namespaces.add_line(brewery_id, "二号糖化线", 120.0, 8)
            recipe_id = published_recipe(app)
            results: list[dict] = []
            errors: list[Exception] = []

            def launch() -> None:
                try:
                    view = app.registry.brewing.create_batch(recipe_id, 1000.0, "tester")
                    results.append(view["batch"])
                except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                    errors.append(exc)

            threads = [threading.Thread(target=launch) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertEqual([], [str(error) for error in errors])
            codes = [item["code"] for item in results]
            self.assertEqual(8, len(codes))
            self.assertEqual(len(codes), len(set(codes)), f"批次号重复: {codes}")
            line_ids = {item["line_id"] for item in results}
            self.assertEqual(2, len(line_ids), "容器位应先占满一线再溢出到二线")
        finally:
            app.close()


class TankBindingTest(unittest.TestCase):
    """发酵罐按罐互斥，且操作不得跨批次串罐。"""

    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.brewing = self.app.registry.brewing

    def tearDown(self) -> None:
        self.app.close()

    def _cooled_batch(self) -> str:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        boil_to_cooling(self.app, batch_id)
        self.brewing.mark_cooled(batch_id, 10.0, "tester")
        return batch_id

    def test_batch_cannot_fill_two_tanks(self) -> None:
        batch_id = self._cooled_batch()
        tanks = self.app.registry.tanks.list_tanks()
        first_tank_id, second_tank_id = str(tanks[0]["id"]), str(tanks[1]["id"])
        sanitize_tank(self.app, first_tank_id)
        sanitize_tank(self.app, second_tank_id)
        self.brewing.transfer_to_tank(batch_id, first_tank_id, "tester")
        with self.assertRaises(ConflictError):
            self.brewing.transfer_to_tank(batch_id, second_tank_id, "tester")
        # 第二只罐仍然空闲，未被这次非法操作占用
        self.assertEqual("sanitized", self.app.registry.tanks.get(second_tank_id)["stage"])

    def test_pitch_and_mature_reject_foreign_tank(self) -> None:
        batch_id = self._cooled_batch()
        tanks = self.app.registry.tanks.list_tanks()
        first_tank_id, second_tank_id = str(tanks[0]["id"]), str(tanks[1]["id"])
        sanitize_tank(self.app, first_tank_id)
        self.brewing.transfer_to_tank(batch_id, first_tank_id, "tester")
        with self.assertRaises(ConflictError):
            self.brewing.pitch_yeast(batch_id, second_tank_id, 10.0, 900.0, "tester")
        self.brewing.pitch_yeast(batch_id, first_tank_id, 10.0, 900.0, "tester")
        with self.assertRaises(ConflictError):
            self.brewing.mature_batch(batch_id, second_tank_id, 14.0, "tester")
        self.assertEqual("fermenting", self.brewing.status(batch_id)["batch"]["stage"])


class CipCircuitExclusionTest(unittest.TestCase):
    """CIP 回路全局互斥：同一回路同一时刻只能清洗一只罐。"""

    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.maintenance = self.app.registry.maintenance

    def tearDown(self) -> None:
        self.app.close()

    def test_circuit_accepts_one_cycle_at_a_time(self) -> None:
        tanks = self.app.registry.tanks.list_tanks()
        first_tank_id, second_tank_id = str(tanks[0]["id"]), str(tanks[1]["id"])
        cycle = self.maintenance.start_clean(first_tank_id, "tester")
        with self.assertRaises(ConflictError):
            self.maintenance.start_clean(second_tank_id, "tester")
        self.maintenance.finish_clean(str(cycle["id"]), "tester")
        follow_up = self.maintenance.start_clean(second_tank_id, "tester")
        self.assertEqual("prerinse", follow_up["stage"])

    def test_tank_level_check_still_applies(self) -> None:
        tank_id = first_tank(self.app)
        self.maintenance.start_clean(tank_id, "tester")
        with self.assertRaises(ConflictError):
            self.maintenance.start_clean(tank_id, "tester")


class CreateRollbackTest(unittest.TestCase):
    """创建批次失败时必须完整回滚，不留下会影响其他批次的残留。"""

    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.brewing = self.app.registry.brewing

    def tearDown(self) -> None:
        self.app.close()

    def test_failed_create_leaves_no_trace(self) -> None:
        registry = self.app.registry
        brewery_id = str(registry.namespaces.list_breweries()[0]["id"])
        # 糖化第一步 98°C 超出糖化锅允许上限（95°C），创建必然在 mash.start 失败
        recipe = registry.recipes.define(
            name="烫水试验",
            style="Test",
            brewery_id=brewery_id,
            volume_l=1000.0,
            boil_minutes=60.0,
            og_target=1.052,
            fg_target=1.012,
            ibu_target=30.0,
            mash_steps=[{"name": "烫水", "target_temp_c": 98.0, "minutes": 10.0}],
            hop_schedule=[
                {"name": "Magnum", "amount_g": 100.0, "window_start_min": 0.0, "window_end_min": 5.0}
            ],
        )
        registry.recipes.publish(str(recipe["id"]))
        with self.assertRaises(ValidationError):
            self.brewing.create_batch(str(recipe["id"]), 1000.0, "tester")

        self.assertEqual(0, self.brewing.summary()["total"])
        self.assertEqual(0, registry.mash.runs.count())
        self.assertEqual(0, registry.wort.runs.count())
        self.assertEqual(0, registry.hops.additions.count())
        self.assertEqual(0, registry.namespaces.usage(brewery_id)["active"])
        for line in registry.namespaces.vessel_usage(brewery_id)["lines"]:
            self.assertEqual(0, line["active"])
        # 回滚后立即可用合法配方开新批，编号不复用残留状态
        batch_id = create_batch(self.app)
        self.assertEqual("mashing", self.brewing.status(batch_id)["batch"]["stage"])

    def test_filter_mash_validation_failure_is_retryable(self) -> None:
        batch_id = create_batch(self.app)
        self.brewing.confirm_water(batch_id, 52.0, "probe-mash", "tester")
        self.brewing.charge_mash(batch_id, 220.0, "tester")
        self.brewing.heat_mash(batch_id, 65.0, "tester")
        self.brewing.rest_mash(batch_id, 65.0, "tester")
        with self.assertRaises(ValidationError):
            self.brewing.filter_mash(batch_id, 9.9, 1020.0, 5.4, "tester")
        # 校验失败不落任何状态，修正参数后可以重试
        self.assertEqual("resting", self.brewing.status(batch_id)["mash"]["stage"])
        view = self.brewing.filter_mash(batch_id, 5.2, 1020.0, 5.4, "tester")
        self.assertEqual("boiling", view["batch"]["stage"])


if __name__ == "__main__":
    unittest.main()
