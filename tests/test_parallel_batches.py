"""多批次并行：设备互斥、罐体归属与批次隔离。"""

from __future__ import annotations

import threading
import unittest

from breweryctl.core.errors import ConflictError, QuotaExceededError, SequenceError, ValidationError

from .helpers import (
    StepClock,
    boil_to_cooling,
    create_batch,
    first_tank,
    make_app,
    mash_to_filter,
    sanitize_tank,
)


def brewery_id(app) -> str:
    return str(app.registry.namespaces.list_breweries()[0]["id"])


def tanks(app) -> list[str]:
    return [str(item["id"]) for item in app.registry.tanks.list_tanks()]


def add_vessel(app, kind: str) -> None:
    app.registry.equipment.register_vessel(brewery_id(app), kind)


def to_cooling(app, batch_id: str) -> None:
    mash_to_filter(app, batch_id)
    boil_to_cooling(app, batch_id)
    app.registry.brewing.mark_cooled(batch_id, 10.0, "tester")


def to_tank(app, batch_id: str, tank_id: str) -> None:
    to_cooling(app, batch_id)
    sanitize_tank(app, tank_id)
    app.registry.brewing.transfer_to_tank(batch_id, tank_id, "tester")


class VesselExclusionTest(unittest.TestCase):
    """糖化锅与煮沸锅同一时刻只能服务一个批次。"""

    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.brewing = self.app.registry.brewing

    def test_second_batch_waits_for_mash_tun(self) -> None:
        batch_a = create_batch(self.app)
        view = self.brewing.status(batch_a)
        self.assertEqual("mash_tun", view["equipment"][0]["kind"])
        with self.assertRaises(ConflictError):
            create_batch(self.app)
        mash_to_filter(self.app, batch_a)
        batch_b = create_batch(self.app)
        self.assertEqual("boiling", self.brewing.status(batch_a)["batch"]["stage"])
        self.assertEqual("mashing", self.brewing.status(batch_b)["batch"]["stage"])

    def test_boil_kettle_is_exclusive(self) -> None:
        add_vessel(self.app, "mash_tun")
        batch_a = create_batch(self.app)
        batch_b = create_batch(self.app)
        for batch_id in (batch_a, batch_b):
            self.brewing.confirm_water(batch_id, 52.0, "probe-mash", "tester")
            self.brewing.charge_mash(batch_id, 220.0, "tester")
            self.brewing.heat_mash(batch_id, 65.0, "tester")
            self.brewing.rest_mash(batch_id, 65.0, "tester")
        self.brewing.filter_mash(batch_a, 5.2, 1020.0, 5.4, "tester")
        with self.assertRaises(ConflictError):
            self.brewing.filter_mash(batch_b, 5.2, 1020.0, 5.4, "tester")
        self.assertEqual("resting", self.brewing.status(batch_b)["mash"]["stage"])
        boil_to_cooling(self.app, batch_a)
        self.brewing.filter_mash(batch_b, 5.2, 1020.0, 5.4, "tester")
        self.assertEqual("boiling", self.brewing.status(batch_b)["batch"]["stage"])

    def test_batch_codes_are_unique(self) -> None:
        add_vessel(self.app, "mash_tun")
        batch_a = create_batch(self.app)
        batch_b = create_batch(self.app)
        code_a = self.brewing.status(batch_a)["batch"]["code"]
        code_b = self.brewing.status(batch_b)["batch"]["code"]
        self.assertNotEqual(code_a, code_b)

    def test_quota_error_leaves_no_residue(self) -> None:
        app = make_app(clock=StepClock(), max_active_batches=1)
        brewing = app.registry.brewing
        create_batch(app)
        with self.assertRaises(QuotaExceededError):
            create_batch(app)
        self.assertEqual(1, brewing.summary()["total"])
        self.assertEqual(1, app.registry.equipment.summary()["occupied"])
        self.assertEqual(1, app.registry.mash.summary()["total"])


class TankOwnershipTest(unittest.TestCase):
    """批次只能操作自己占用的发酵罐。"""

    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        add_vessel(self.app, "mash_tun")
        add_vessel(self.app, "boil_kettle")
        self.brewing = self.app.registry.brewing
        self.tank_a, self.tank_b = tanks(self.app)

    def test_pitch_rejects_foreign_tank(self) -> None:
        batch_a = create_batch(self.app)
        batch_b = create_batch(self.app)
        to_tank(self.app, batch_a, self.tank_a)
        to_tank(self.app, batch_b, self.tank_b)
        with self.assertRaises(ValidationError):
            self.brewing.pitch_yeast(batch_b, self.tank_a, 10.0, 1000.0, "tester")
        tank = self.app.registry.tanks.get(self.tank_a)
        self.assertEqual("filled", tank["stage"])
        self.assertEqual(batch_a, tank["batch_id"])

    def test_mature_rejects_foreign_tank(self) -> None:
        batch_a = create_batch(self.app)
        batch_b = create_batch(self.app)
        to_tank(self.app, batch_a, self.tank_a)
        to_tank(self.app, batch_b, self.tank_b)
        self.brewing.pitch_yeast(batch_a, self.tank_a, 10.0, 1000.0, "tester")
        self.brewing.pitch_yeast(batch_b, self.tank_b, 10.0, 1000.0, "tester")
        with self.assertRaises(ValidationError):
            self.brewing.mature_batch(batch_a, self.tank_b, 14.0, "tester")
        tank = self.app.registry.tanks.get(self.tank_b)
        self.assertEqual("fermenting", tank["stage"])
        self.assertEqual(batch_b, tank["batch_id"])
        view = self.brewing.mature_batch(batch_a, self.tank_a, 14.0, "tester")
        self.assertEqual("maturing", view["batch"]["stage"])

    def test_transfer_rejects_other_brewery_tank(self) -> None:
        other = self.app.registry.namespaces.register_brewery("二分厂", "second")
        foreign = self.app.registry.tanks.register_tank(str(other["id"]), 9, 1200.0)
        batch_a = create_batch(self.app)
        to_cooling(self.app, batch_a)
        with self.assertRaises(ValidationError):
            self.brewing.transfer_to_tank(batch_a, str(foreign["id"]), "tester")


class CipExclusionTest(unittest.TestCase):
    """CIP 回路互斥与罐体占用保护。"""

    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.maintenance = self.app.registry.maintenance
        self.tank_a, self.tank_b = tanks(self.app)

    def test_circuit_allows_one_active_cycle(self) -> None:
        cycle = self.maintenance.start_clean(self.tank_a, "tester")
        self.assertEqual("cleaning", self.app.registry.tanks.get(self.tank_a)["stage"])
        with self.assertRaises(ConflictError):
            self.maintenance.start_clean(self.tank_b, "tester")
        self.assertEqual("idle", self.app.registry.tanks.get(self.tank_b)["stage"])
        self.maintenance.finish_clean(str(cycle["id"]), "tester")
        follow_up = self.maintenance.start_clean(self.tank_b, "tester")
        self.assertEqual(self.tank_b, follow_up["tank_id"])

    def test_occupied_tank_cannot_be_cleaned(self) -> None:
        batch_a = create_batch(self.app)
        to_tank(self.app, batch_a, self.tank_a)
        with self.assertRaises(ConflictError):
            self.maintenance.start_clean(self.tank_a, "tester")
        tank = self.app.registry.tanks.get(self.tank_a)
        self.assertEqual("filled", tank["stage"])
        self.assertEqual(batch_a, tank["batch_id"])

    def test_tank_cannot_join_two_circuits(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.registry.cip.register_circuit(brewery_id(self.app), 2, [self.tank_a], 10.0)


class AbortReleaseTest(unittest.TestCase):
    """中止批次必须释放它占用的全部共享资源。"""

    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.brewing = self.app.registry.brewing

    def test_abort_releases_mash_tun(self) -> None:
        batch_a = create_batch(self.app)
        with self.assertRaises(ConflictError):
            create_batch(self.app)
        self.brewing.abort_batch(batch_a, "设备点检", "tester")
        batch_b = create_batch(self.app)
        self.assertEqual("mashing", self.brewing.status(batch_b)["batch"]["stage"])
        self.assertEqual(1, self.app.registry.equipment.summary()["occupied"])

    def test_abort_releases_tank_and_quota(self) -> None:
        batch_a = create_batch(self.app)
        tank_a = first_tank(self.app)
        to_tank(self.app, batch_a, tank_a)
        self.brewing.abort_batch(batch_a, "麦汁污染", "tester")
        tank = self.app.registry.tanks.get(tank_a)
        self.assertEqual("idle", tank["stage"])
        self.assertIsNone(tank["batch_id"])
        self.assertIsNone(tank["cip_certificate_id"])
        usage = self.app.registry.namespaces.usage(brewery_id(self.app))
        self.assertEqual(0, usage["active"])


class ConcurrentAccessTest(unittest.TestCase):
    """真实线程并发下的隔离保证。"""

    def test_concurrent_creates_get_unique_codes_and_vessels(self) -> None:
        app = make_app(clock=StepClock())
        for _ in range(3):
            add_vessel(app, "mash_tun")
        results: list[str] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(create_batch(app))
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual([], errors)
        self.assertEqual(4, len(set(results)))
        codes = [app.registry.brewing.status(item)["batch"]["code"] for item in results]
        self.assertEqual(4, len(set(codes)))
        self.assertEqual(4, app.registry.equipment.summary()["occupied"])
        usage = app.registry.namespaces.usage(brewery_id(app))
        self.assertEqual(4, usage["active"])

    def test_concurrent_same_batch_op_runs_once(self) -> None:
        app = make_app(clock=StepClock())
        batch_id = create_batch(app)
        outcomes: list[str] = []

        def worker() -> None:
            try:
                app.registry.brewing.confirm_water(batch_id, 52.0, "probe-mash", "tester")
                outcomes.append("ok")
            except SequenceError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(["ok", "rejected"], sorted(outcomes))
        view = app.registry.brewing.status(batch_id)
        self.assertEqual("water_confirmed", view["mash"]["stage"])


class InterleavedPipelineTest(unittest.TestCase):
    """两个批次交错走完全流程，状态互不污染。"""

    def test_two_batches_full_pipeline(self) -> None:
        clock = StepClock()
        app = make_app(clock=clock)
        add_vessel(app, "mash_tun")
        add_vessel(app, "boil_kettle")
        brewing = app.registry.brewing
        tank_a, tank_b = tanks(app)

        batch_a = create_batch(app)
        batch_b = create_batch(app)
        for batch_id in (batch_a, batch_b):
            brewing.confirm_water(batch_id, 52.0, "probe-mash", "tester")
        for batch_id in (batch_a, batch_b):
            brewing.charge_mash(batch_id, 220.0, "tester")
            brewing.heat_mash(batch_id, 65.0, "tester")
            brewing.rest_mash(batch_id, 65.0, "tester")
        brewing.filter_mash(batch_a, 5.2, 1020.0, 5.4, "tester")
        brewing.filter_mash(batch_b, 5.4, 1010.0, 5.5, "tester")
        self.assertEqual(2, app.registry.equipment.summary()["occupied"])

        for batch_id in (batch_a, batch_b):
            brewing.ignite_boil(batch_id, "tester")
            brewing.boil_rolling(batch_id, "tester")
        brewing.add_hop(batch_a, 1, 1.0, "tester")
        brewing.add_hop(batch_b, 1, 2.0, "tester")
        brewing.add_hop(batch_a, 2, 47.0, "tester")
        brewing.add_hop(batch_b, 2, 48.0, "tester")
        brewing.add_hop(batch_a, 3, 55.0, "tester")
        brewing.add_hop(batch_b, 3, 56.0, "tester")
        clock.advance(60)
        brewing.whirlpool(batch_a, 59.0, "tester")
        brewing.whirlpool(batch_b, 59.0, "tester")
        self.assertEqual(0, app.registry.equipment.summary()["occupied"])

        view_a = brewing.status(batch_a)
        view_b = brewing.status(batch_b)
        self.assertEqual(3, view_a["hops"]["added"])
        self.assertEqual(3, view_b["hops"]["added"])
        self.assertIsNone(view_a["hops"]["next_position"])
        self.assertNotEqual(view_a["batch"]["code"], view_b["batch"]["code"])

        brewing.mark_cooled(batch_a, 10.0, "tester")
        brewing.mark_cooled(batch_b, 10.5, "tester")
        sanitize_tank(app, tank_a)
        sanitize_tank(app, tank_b)
        brewing.transfer_to_tank(batch_a, tank_a, "tester")
        brewing.transfer_to_tank(batch_b, tank_b, "tester")
        brewing.pitch_yeast(batch_a, tank_a, 10.0, 1000.0, "tester")
        brewing.pitch_yeast(batch_b, tank_b, 10.0, 1000.0, "tester")
        self.assertEqual(batch_a, app.registry.tanks.get(tank_a)["batch_id"])
        self.assertEqual(batch_b, app.registry.tanks.get(tank_b)["batch_id"])

        brewing.mature_batch(batch_a, tank_a, 14.0, "tester")
        brewing.mature_batch(batch_b, tank_b, 14.0, "tester")
        brewing.complete_batch(batch_a, "tester")
        brewing.complete_batch(batch_b, "tester")
        self.assertEqual("completed", brewing.status(batch_a)["batch"]["stage"])
        self.assertEqual("completed", brewing.status(batch_b)["batch"]["stage"])

        usage = app.registry.namespaces.usage(brewery_id(app))
        self.assertEqual(0, usage["active"])
        audit_a = brewing.audit_history(batch_a)
        self.assertTrue(all(entry["batch_id"] == batch_a for entry in audit_a["entries"]))
        self.assertIn("batch.created", audit_a["actions"])
