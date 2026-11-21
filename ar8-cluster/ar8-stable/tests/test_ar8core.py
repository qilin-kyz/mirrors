"""AR8 稳定核心回归测试 —— 纯离线、确定性、无需集群、无需故障注入。

对齐 PVE pve-ha-manager/src/test 的做法：把故障场景变成数据 + 期望断言，
而不是每次靠人去 iptables 注入、看日志、手工修 bug。
"""
import copy
import glob
import json
import os
import sys
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ar8core as core  # noqa: E402
import ar8ctl  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(ROOT, "ar8.config.yaml"), encoding="utf-8"))
# 场景已随「混沌韧性测试」模块下线，不再以文件形式分发；
# 但状态机在故障下的正确性是 ar8core 的核心价值，故把场景内嵌进测试。
# 场景格式与 ar8core.simulate 一致：faults = [{at: 虚拟tick, cmd: SimEnv命令}]。
# 派单/租约/收割由 reconcile 自动驱动，场景只负责在指定时刻注入故障。
BUILTIN_SCENARIOS = {
    "02-single-node-partition": {
        "name": "single-node-partition",
        "n": 8000, "chunk": 2000, "work_ticks": 4, "max_ticks": 200,
        "faults": [
            {"at": 2, "cmd": "network node1 off"},
            {"at": 12, "cmd": "network node1 on"},
        ],
    },
    "05-multi-node-no-quorum": {
        "name": "multi-node-no-quorum",
        "n": 8000, "chunk": 2000, "work_ticks": 4, "max_ticks": 200,
        "faults": [
            {"at": 2, "cmd": "cfs fail"},
            {"at": 8, "cmd": "cfs work"},
        ],
    },
    "08-duplicate-submit": {
        "name": "duplicate-submit",
        "n": 8000, "chunk": 2000, "work_ticks": 4, "max_ticks": 200,
        "faults": [
            {"at": 2, "cmd": "duplicate"},
        ],
    },
    "09-stale-generation": {
        "name": "stale-generation",
        "n": 8000, "chunk": 2000, "work_ticks": 4, "max_ticks": 200,
        "faults": [
            {"at": 2, "cmd": "stale"},
        ],
    },
    "11-lease-expiry-requeue": {
        "name": "lease-expiry-requeue",
        "n": 8000, "chunk": 2000, "work_ticks": 4, "max_ticks": 200,
        "faults": [
            # worker 卡死（结果永不产生）-> 必须触发 worker-deadline 作废重派
            {"at": 1, "cmd": "slow 1000"},
            {"at": 25, "cmd": "normal"},
        ],
    },
}


def load_scenario(name):
    """按名字取内置场景。"""
    key = os.path.basename(name)
    if key.endswith(".json"):
        key = key[:-len(".json")]
    if key not in BUILTIN_SCENARIOS:
        raise KeyError(f"未知内置场景 {key}；可用: {sorted(BUILTIN_SCENARIOS)}")
    return copy.deepcopy(BUILTIN_SCENARIOS[key])


def cfg_copy():
    return copy.deepcopy(CFG)


class TestSchema(unittest.TestCase):
    def test_01_shipped_config_is_valid(self):
        self.assertEqual(core.validate_config(CFG), [])

    def test_02_schema_spec_selfcheck(self):
        # 类比 PVE::API2::verifyapi：声明本身的完整性也要被测试，防止手改漏项
        for path, spec in core.FIELD_SPEC.items():
            self.assertEqual(len(spec), 3, f"{path} spec must be (pred, desc, constraint)")
            pred, desc, constraint = spec
            self.assertTrue(callable(pred), f"{path} pred not callable")
            self.assertIsInstance(desc, str, f"{path} desc missing")
            self.assertTrue(callable(constraint), f"{path} constraint not callable")

    def test_03_missing_required_field_rejected(self):
        c = cfg_copy()
        del c["generation"]
        self.assertIn("missing field: generation", core.validate_config(c))

    def test_04_bad_type_rejected(self):
        c = cfg_copy()
        c["features"]["job"]["chunk"] = "two thousand"
        self.assertTrue(any("bad type: features.job.chunk" in x for x in core.validate_config(c)))

    def test_05_duplicate_port_rejected(self):
        c = cfg_copy()
        # 制造端口冲突（jobBroker 与 s3 同端口）应被拒绝
        c["ports"]["jobBroker"] = c["ports"]["s3"]
        self.assertTrue(any("constraint failed: ports" in x for x in core.validate_config(c)))

    def test_06_duplicate_node_id_rejected(self):
        c = cfg_copy()
        c["nodes"][1]["id"] = c["nodes"][0]["id"]
        self.assertIn("duplicate node id", core.validate_config(c))

    def test_07_exactly_one_control_plane(self):
        c = cfg_copy()
        c["nodes"][1]["role"] = "control-plane"
        self.assertTrue(any("exactly one control-plane" in x for x in core.validate_config(c)))

    def test_08_object_storage_must_not_be_block_mountable(self):
        c = cfg_copy()
        c["storage"][2]["mountableAsBlock"] = True
        self.assertTrue(any("must declare mountableAsBlock=false" in x for x in core.validate_config(c)))

    def test_09_cluster_wide_mount_must_be_forbidden(self):
        c = cfg_copy()
        c["storage"][0]["clusterWideMount"] = "allowed"
        self.assertTrue(any("must forbid cluster-wide mount" in x for x in core.validate_config(c)))

    def test_10_illegal_fsm_target_rejected(self):
        c = cfg_copy()
        c["stateMachines"]["shard"]["transitions"]["queued"].append("harvested")
        # queued->harvested 本身是合法枚举内目标，但状态机的自洽检查要能发现未知目标
        c["stateMachines"]["shard"]["transitions"]["accepted"].append("ghost")
        self.assertTrue(any("FSM transition to unknown state" in x for x in core.validate_config(c)))


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.sm = core.StateMachine("shard", CFG["stateMachines"]["shard"])

    def test_11_legal_transition(self):
        obj = dict(id=0, state="queued", ticks_in_state=0)
        self.assertTrue(self.sm.move(obj, "leased", 1, "test"))
        self.assertEqual(obj["state"], "leased")

    def test_12_illegal_transition_raises(self):
        obj = dict(id=0, state="queued", ticks_in_state=0)
        with self.assertRaises(core.StateError):
            self.sm.move(obj, "harvested", 1, "test")

    def test_13_same_state_is_noop(self):
        obj = dict(id=0, state="queued", ticks_in_state=0)
        self.assertFalse(self.sm.move(obj, "queued", 1, "test"))

    def test_14_terminal_state_has_no_outgoing(self):
        self.assertFalse(self.sm.legal("harvested", "queued"))


class TestFencing(unittest.TestCase):
    def test_15_current_generation_accepted(self):
        f = core.Fence("runA", 7)
        ok, reason = f.accept(dict(run="runA", generation=7, uid="u1"))
        self.assertTrue(ok, reason)

    def test_16_stale_generation_rejected(self):
        f = core.Fence("runA", 7)
        ok, reason = f.accept(dict(run="runA", generation=6, uid="u1"))
        self.assertFalse(ok)
        self.assertEqual(reason, "stale-generation")

    def test_17_wrong_run_rejected(self):
        f = core.Fence("runA", 7)
        ok, _ = f.accept(dict(run="runB", generation=7, uid="u1"))
        self.assertFalse(ok)

    def test_18_duplicate_uid_rejected(self):
        f = core.Fence("runA", 7)
        cmd = dict(run="runA", generation=7, uid="u1")
        self.assertTrue(f.accept(cmd)[0])
        self.assertEqual(f.accept(cmd)[1], "duplicate")

    def test_19_missing_uid_rejected(self):
        f = core.Fence("runA", 7)
        self.assertEqual(f.accept(dict(run="runA", generation=7))[1], "missing-uid")

    def test_20_uid_is_deterministic_and_unique(self):
        a = core.command_uid("r", 1, 3, 9)
        b = core.command_uid("r", 1, 3, 9)
        c = core.command_uid("r", 1, 3, 10)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(core.fencing_token("r", 1, "pilot"),
                            core.fencing_token("r", 1, "node1"))


class TestReferenceImplementation(unittest.TestCase):
    def test_21_reference_matches_known_small_range(self):
        # [2,10]: primes 2,3,5,7 -> pc=4 ps=17 pf=2 pl=7
        r = core.reference_shard(2, 10)
        self.assertEqual((r["pc"], r["ps"], r["pf"], r["pl"]), (4, 17, 2, 7))
        # d(x) 之和：1+2+2+3+2+4+2+4+3 (x=1..9 去掉 x=1 的 1) 用公式独立校验
        self.assertEqual(r["dc"], sum(10 // d - 1 // d for d in range(1, 11)))

    def test_22_shards_cover_range_without_gap_or_overlap(self):
        sh = core.make_shards(10000, 2000)
        self.assertEqual(sh[0]["lo"], 2)
        self.assertEqual(sh[-1]["hi"], 10000)
        for a, b in zip(sh, sh[1:]):
            self.assertEqual(a["hi"] + 1, b["lo"])

    def test_23_aggregate_equals_whole_range_reference(self):
        sh = core.make_shards(5000, 500)
        got = core.aggregate(core.reference_shard(s["lo"], s["hi"]) for s in sh)
        self.assertEqual(got, core.reference_shard(2, 5000))


class TestManifestAndPlan(unittest.TestCase):
    def test_24_manifest_is_single_source_and_deterministic(self):
        d1 = ar8ctl.desired_manifest(CFG)
        d2 = ar8ctl.desired_manifest(CFG)
        self.assertEqual(ar8ctl.manifest_fingerprint(d1), ar8ctl.manifest_fingerprint(d2))
        self.assertEqual(len(d1), 8)
        self.assertEqual(sum(1 for x in d1 if x["kind"] == "StorageClass"), 4)

    def test_25_manifest_has_no_embedded_shell(self):
        # 旧系统把 56 段 shell 内嵌进 YAML；新清单不允许任何脚本块
        text = ar8ctl.manifest_text(ar8ctl.desired_manifest(CFG))
        self.assertNotIn("/bin/sh", text)
        self.assertNotIn("httpd -f", text)
        self.assertNotIn("flock", text)

    def test_26_object_storage_policy_present(self):
        d = ar8ctl.desired_manifest(CFG)
        vaps = [x for x in d if x["kind"] == "ValidatingAdmissionPolicy"]
        self.assertEqual(len(vaps), 1)
        expr = " ".join(v["expression"] for v in vaps[0]["spec"]["validations"])
        self.assertIn("ReadWriteMany", expr)
        self.assertIn("ar8.io/scope", expr)

    def test_27_config_change_changes_fingerprint(self):
        c = cfg_copy()
        c["generation"] = 2
        self.assertNotEqual(ar8ctl.manifest_fingerprint(ar8ctl.desired_manifest(c)),
                            ar8ctl.manifest_fingerprint(ar8ctl.desired_manifest(CFG)))

    def test_28_generated_job_is_native_k8s_job(self):
        job = dict(name="ar8-shard-r-0", shard=0, lo=2, hi=2001,
                   run="r", generation=1, uid="abc", node="pilot")
        d = ar8ctl.job_manifest(CFG, job, ar8ctl.AWK_SHARD)
        self.assertEqual(d["kind"], "Job")
        self.assertEqual(d["spec"]["restartPolicy"] if "restartPolicy" in d["spec"]
                         else d["spec"]["template"]["spec"]["restartPolicy"], "OnFailure")
        self.assertTrue(all(env["value"] for env in
                            d["spec"]["template"]["spec"]["containers"][0]["env"]))


class TestLedger(unittest.TestCase):
    def test_29_snapshot_restore_preserves_fencing(self):
        shards = core.make_shards(4000, 2000)
        led = core.Ledger("runX", 5, shards)
        led.fence.accept(dict(run="runX", generation=5, uid="u1"))
        led.accepted[0] = dict(uid="u1", metrics=core.reference_shard(2, 2001))
        snap = led.snapshot()
        led2 = core.Ledger.restore(json.loads(json.dumps(snap)), shards)
        self.assertEqual(led2.fence.accept(dict(run="runX", generation=5, uid="u1"))[1],
                         "duplicate")
        self.assertEqual(led2.generation, 5)


class TestScenarios(unittest.TestCase):
    def test_30_all_scenarios_converge_with_zero_violations(self):
        self.assertGreaterEqual(len(BUILTIN_SCENARIOS), 5)
        for key in sorted(BUILTIN_SCENARIOS):
            sc = load_scenario(key)
            r = core.simulate(CFG, sc)
            with self.subTest(scenario=sc["name"]):
                self.assertEqual(r["violations"], [], f"{sc['name']}: {r['violations']}")
                self.assertTrue(r["summary"]["converged"])
                self.assertEqual(r["summary"]["harvested"], r["summary"]["total"])
                self.assertEqual(r["accepted"], r["summary"]["total"])

    def test_31_simulation_is_replay_deterministic(self):
        sc = load_scenario("02-single-node-partition.json")
        r1 = core.simulate(CFG, sc)
        r2 = core.simulate(CFG, sc)
        self.assertEqual(r1["fingerprint"], r2["fingerprint"])
        self.assertEqual(r1["events"], r2["events"])

    def test_32_duplicate_scenario_actually_suppresses(self):
        sc = load_scenario("08-duplicate-submit.json")
        r = core.simulate(CFG, sc)
        self.assertGreater(r["dup_suppressed"], 0)
        self.assertEqual(r["accepted"], r["summary"]["total"])

    def test_33_stale_generation_is_rejected_but_still_converges(self):
        sc = load_scenario("09-stale-generation.json")
        r = core.simulate(CFG, sc)
        self.assertIn("stale-generation", r["rejected"])
        self.assertTrue(r["summary"]["converged"])

    def test_34_no_quorum_scenario_freezes_then_recovers(self):
        sc = load_scenario("05-multi-node-no-quorum.json")
        r = core.simulate(CFG, sc)
        self.assertTrue(any("NO-QUORUM" in e for e in r["events"]))
        self.assertTrue(r["summary"]["converged"])

    def test_35_invariants_catch_a_broken_result(self):
        shards = core.make_shards(4000, 2000)
        led = core.Ledger("broken", 1, shards)
        for s in shards:
            led.accepted[s["id"]] = dict(uid=core.command_uid("broken", 1, s["id"], 1),
                                         metrics=dict(pc=0, ps=0, pf=0, pl=0, dc=0))
        summary = dict(ticks=1, converged=True, harvested=len(shards), total=len(shards))
        bad = core.check_invariants(CFG, led, summary)
        self.assertTrue(any("allAcceptedSumMatchesReference" in x for x in bad))

    def test_37_worker_deadline_prevents_silent_stall(self):
        # PVE 的反面教训：upid_wait 无 deadline -> worker 卡死静默不自愈。
        # AR8 必须有显式 ALERT + 作废 uid + 重新派单，且最终收敛。
        sc = load_scenario("11-lease-expiry-requeue.json")
        r = core.simulate(CFG, sc)
        self.assertTrue(any("ALERT worker-deadline" in e for e in r["events"]))
        self.assertTrue(any("invalidate-and-redispatch" in e for e in r["events"]))
        self.assertTrue(r["summary"]["converged"])
        self.assertEqual(r["violations"], [])

    def test_38_no_silent_stall_invariant_is_enforced(self):
        shards = core.make_shards(4000, 2000)
        led = core.Ledger("stalled", 1, shards)
        led.shards[0]["state"] = "leased"
        led.shards[0]["ticks_in_state"] = 99
        summary = dict(ticks=99, converged=False, harvested=1, total=len(shards))
        bad = core.check_invariants(CFG, led, summary)
        self.assertTrue(any("noSilentStall" in x for x in bad))

    def test_36_invariants_catch_shard_loss(self):
        shards = core.make_shards(4000, 2000)
        led = core.Ledger("loss", 1, shards)
        summary = dict(ticks=1, converged=False, harvested=1, total=len(shards))
        bad = core.check_invariants(CFG, led, summary)
        self.assertTrue(any("noShardLost" in x for x in bad))
        self.assertTrue(any("nodesConvergeWithinTicks" in x for x in bad))

    # --- PVE 源码级增强的回归（dcdb.c / pmxcfs.c / quorum.c） ---

    def test_39_structure_rejects_empty_manifest(self):
        # dcdb.c:636 拒绝 size==0，否则所有 follower 会丢光 inode
        bad = core.validate_structure([], CFG)
        self.assertTrue(any("manifest is empty" in x for x in bad))

    def test_40_structure_rejects_stale_generation(self):
        docs = [{
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "ar8-config"},
            "data": {"generation": str(CFG["generation"] + 1)},
        }] + [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": f"n{i}"}}
            for i in range(len(CFG["nodes"]))
        ]
        bad = core.validate_structure(docs, CFG)
        self.assertTrue(any("stale generation" in x for x in bad))

    def test_41_select_leader_is_deterministic_by_version_then_mtime(self):
        cands = [
            {"id": "a", "version": 3, "mtime": 1},
            {"id": "b", "version": 3, "mtime": 9},
            {"id": "c", "version": 2, "mtime": 99},
        ]
        self.assertEqual(core.select_leader(cands)["id"], "b")
        self.assertEqual(core.select_leader(list(reversed(cands)))["id"], "b")
        self.assertIsNone(core.select_leader([]))

    def test_42_loopback_node_address_is_rejected(self):
        # pmxcfs.c lookup_node_ip() 明确跳过 127.* / ::1
        bad = copy.deepcopy(CFG)
        bad["cluster"]["pilot"] = "127.0.0.1"
        with self.assertRaises(core.ConfigError):
            core.assert_no_loopback(bad)
        core.assert_no_loopback(CFG)

    def test_43_op_lock_is_exclusive(self):
        # pmxcfs.c:855-872 的单实例 flock，防止并发 apply 撕裂 state
        import fcntl
        import os
        with ar8ctl.op_lock():
            fd = os.open(ar8ctl.LOCK_FILE, os.O_RDWR)
            try:
                with self.assertRaises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_44_late_result_recovered_before_uid_reassign(self):
        # Manager.pm:1609-1611："ensure also no LRM result gets lost"
        # 超时后旧 uid 的结果才到达时，必须先消费它，不能清 uid 换单导致结果丢失。
        cfg = copy.deepcopy(CFG)
        shards = core.make_shards(2000, 2000)
        sid = shards[0]["id"]
        env = core.SimEnv(cfg, work_ticks=1000)  # 结果正常永不产生
        led = core.Ledger("late", 1, shards)
        old_uid = core.command_uid("late", 1, sid, 1)
        led.shards[sid].update(state="expired", uid=old_uid, ticks_in_state=20)
        ref = core.reference_shard(shards[0]["lo"], shards[0]["hi"])
        env.results[sid] = dict(uid=old_uid, node="vm-ar8-node1", metrics=ref,
                                exitCode=0, generation=1, run="late", shard=sid)
        sm = core.StateMachine("shard", cfg["stateMachines"]["shard"])
        core.reconcile(env, cfg, led, sm, max_ticks=1)
        self.assertTrue(any("LATE-RESULT-RECOVERED" in e for e in led.events))
        # 关键断言：uid 没被换掉，结果被消费而不是被丢弃
        self.assertEqual(led.shards[sid]["state"], "submitted")
        self.assertEqual(led.shards[sid]["uid"], old_uid)

    def test_45_wait_task_recognizes_timeout_terminal(self):
        # UPID.pm:87-91 status_is_error() 对无法解析/超时状态一律 fail-closed。
        # 原 wait_task 只认 OK/ERROR，TASK TIMEOUT 会被误当"仍在运行"。
        import tempfile
        upid = "UPID:ar8:deadbeef:00000001:test:"
        old_log = ar8ctl.TASK_LOG
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
                f.write(f"2026-01-01 00:00:00 {upid} BEGIN\n")
                f.write(f"2026-01-01 00:00:01 {upid} TASK TIMEOUT deadline=1s\n")
                path = f.name
            ar8ctl.TASK_LOG = path
            ok, line = ar8ctl.wait_task(upid, timeout=2, interval=0.05)
            self.assertFalse(ok)
            self.assertIn("TASK TIMEOUT", line)
        finally:
            ar8ctl.TASK_LOG = old_log
            import os
            os.unlink(path)

    def test_46_unknown_config_fields_rejected(self):
        # RESTHandler.pm:253-343 注册期严格校验 + JSON Schema 拒绝未知属性。
        # 拼错字段名必须报错，而不是静默走默认值。
        bad = copy.deepcopy(CFG)
        bad["features"]["job"]["chunkSize"] = 2000  # 应为 chunk
        with self.assertRaises(core.ConfigError) as ctx:
            core.assert_no_unknown_fields(bad)
        self.assertIn("chunkSize", str(ctx.exception))
        core.assert_no_unknown_fields(CFG)  # 正常配置不受影响


class TestSmartRouting(unittest.TestCase):
    """智能路由：把多节点拼成一台大电脑，按容量加权而不是轮询分片。"""

    def test_47_router_is_capacity_weighted_not_round_robin(self):
        # 轮询会让 hot 与 warm 各拿一半；容量加权必须让强节点拿更多。
        cfg = copy.deepcopy(CFG)
        for n in cfg["nodes"]:
            n["class"] = "hot" if n["id"] == "node1" else "warm"
            n["memoryMib"] = 4096 if n["id"] == "node1" else 2048
        r = core.Router(cfg)
        picked = [r.pick(["node1", "node2"], 100.0) for _ in range(20)]
        self.assertGreater(picked.count("node1"), picked.count("node2"),
                           f"强节点应承接更多分片，实际 {picked}")
        # 1:1 就是轮询，说明加权根本没生效
        self.assertNotEqual(picked.count("node1"), picked.count("node2"))

    def test_48_routing_is_deterministic(self):
        # 相同输入必须逐字节同分配，否则仿真无法回放、真实集群无法复现。
        cfg = copy.deepcopy(CFG)
        a, b = core.Router(cfg), core.Router(cfg)
        pa = [a.pick(["node1", "node2"], 50.0) for _ in range(12)]
        pb = [b.pick(["node1", "node2"], 50.0) for _ in range(12)]
        self.assertEqual(pa, pb)

    def test_49_offline_node_is_never_picked(self):
        cfg = copy.deepcopy(CFG)
        r = core.Router(cfg)
        picked = {r.pick(["node1"], 10.0) for _ in range(5)}
        self.assertEqual(picked, {"node1"}, "只有 node1 在线时不得分配到 node2")

    def test_50_saturated_node_is_demoted(self):
        # 负载感知：内存/CPU 接近上限的节点应被降权，新分片流向空闲节点。
        cfg = copy.deepcopy(CFG)
        r = core.Router(cfg)
        r.observe_load({"node1": {"memRatio": 0.99, "cpuRatio": 0.99},
                        "node2": {"memRatio": 0.10, "cpuRatio": 0.10}})
        picked = [r.pick(["node1", "node2"], 100.0) for _ in range(12)]
        self.assertGreater(picked.count("node2"), picked.count("node1"),
                           f"饱和节点应被降权，实际 {picked}")

    def test_51_real_manifest_pins_shard_to_routed_node(self):
        # 智能路由必须真正落到 k8s 调度：否则 router 选出的节点只写在 ledger 里，
        # Pod 实际落点仍由默认调度器决定 —— 等于没路由。
        cfg = copy.deepcopy(CFG)
        job = dict(name="ar8-shard-r-1", shard=1, lo=2, hi=1001, node="node1",
                   uid="u1", run="r", generation=1)
        doc = ar8ctl.job_manifest(cfg, job, "true")
        spec = doc["spec"]["template"]["spec"]
        self.assertEqual(spec.get("nodeSelector", {}).get("kubernetes.io/hostname"),
                         "vm-ar8-node1")

    def test_52_cost_model_grows_with_shard_size(self):
        # 代价模型要能识别大分片，否则调度优化没有依据。
        small = core.estimate_shard_cost(2, 1000)
        big = core.estimate_shard_cost(1000, 100000)
        self.assertGreater(big, small * 10)


class TestProfileSemantics(unittest.TestCase):
    """三个调度 profile 的「设计思想是否真实落地」，防装饰性。

    历史教训：上一版「智能路由」只是把结果写进 ledger，Pod 落点仍由 k3s 默认
    调度器决定 —— 等于没路由。所以这里不做「账面好看」，而是断言：
    XNU 的吞吐/延迟分化、OH 的数据本地性，都必须真实改变 pick() 的决策输出。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ar8_profiles_t", os.path.join(ROOT, "ar8-profiles.py"))
        cls.ap = importlib.util.module_from_spec(spec)
        sys.modules["ar8_profiles_t"] = cls.ap
        spec.loader.exec_module(cls.ap)
        cls.base_cfg = {
            "nodes": [
                {"id": "node1", "class": "hot", "memoryMib": 4096},
                {"id": "node2", "class": "hot", "memoryMib": 4096},
            ]
        }

    def test_53_oh_data_locality_changes_decision(self):
        # OH 数据就近：同构、偏斜 home 分布下，任务被派到「数据所在节点」的
        # 命中率必须显著高于 baseline（baseline 不感知 home）。
        # 注意：不能断言「全命中」—— 如果数据全在某个节点，公平积压仍会把
        # 部分任务挤出 home，避免把该节点挤爆（这本身是正确权衡）。
        oh = self.ap.make_router("oh-distributed", self.base_cfg)
        bl = self.ap.make_router("nixos-baseline", self.base_cfg)
        homes = ["node2" if i % 3 != 0 else "node1" for i in range(40)]
        # cost 用确定性 LCG 生成（与 bench-profiles.make_workload 同源），
        # 避免线性递增的规律让 baseline 靠运气命中反而更高
        state = 20261120 & 0x7FFFFFFF
        costs = []
        for _ in range(40):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            hi = int(20 + (state / 0x7FFFFFFF) * 1980)
            costs.append(core.estimate_shard_cost(2, hi))
        def hit_rate(rr):
            hit = 0
            for i in range(40):
                n = rr.pick(["node1", "node2"], costs[i], sid=f"s{i}",
                            tier="utility", home=homes[i])
                if n == homes[i]:
                    hit += 1
            return hit
        oh_hit = hit_rate(oh)
        bl_hit = hit_rate(bl)
        # home 占 2/3，baseline 纯积压只碰运气；OH 必须显著更偏向 home
        self.assertGreater(oh_hit, bl_hit,
                           f"OH 数据就近应提升命中，baseline={bl_hit} OH={oh_hit}")
        self.assertGreater(bl_hit, 0, "baseline 不应感知 home 导致全错")

    def test_54_xnu_throughput_batch_affinity(self):
        # XNU 吞吐导向：background/utility 有 batch 亲和，同节点连跑。
        # 同成本连续派 N 个 utility，落到同一节点的次数应高于「完全交替」。
        xnu = self.ap.make_router("xnu-qos", self.base_cfg)
        picks = [xnu.pick(["node1", "node2"], 50.0, tier="utility")
                 for _ in range(10)]
        # 有 batch 亲和时，不应是严格 node1/node2/node1... 纯交替
        self.assertFalse(all(picks[i] != picks[i + 1] for i in range(len(picks) - 1)),
                         f"batch 亲和应让吞吐任务连续落同一节点，实际 {picks}")

    def test_55_xnu_latency_vs_throughput_differ(self):
        # XNU 延迟敏感（interactive）选「本 QoS 账本积压最小」节点；
        # 吞吐导向（background）有 batch 记忆。两者决策依据不同，绝不该退化。
        xnu = self.ap.make_router("xnu-qos", self.base_cfg)
        # 先给 node1 灌大量 interactive 积压
        for _ in range(5):
            xnu.pick(["node1", "node2"], 500.0, tier="interactive")
        # 此时 interactive 应转向积压更小的 node2
        nxt = xnu.pick(["node1", "node2"], 500.0, tier="interactive")
        self.assertEqual(nxt, "node2", "延迟敏感任务应离开积压高的节点")

    def test_56_oh_trust_penalty_after_failure(self):
        # OH 故障规避：刚恢复的节点在信任窗口内被加价，任务暂不塞回。
        oh = self.ap.make_router("oh-distributed", self.base_cfg)
        oh.set_tick(10)
        oh.note_failure("node1", 10)   # node1 此刻刚异常
        # 同成本、无 home，此时 node1 应因信任惩罚被 node2 抢走
        n = oh.pick(["node1", "node2"], 100.0, tier="default")
        self.assertEqual(n, "node2", "刚异常节点应被信任降级绕开")


class TestTripleGapSemantics(unittest.TestCase):
    """geo（矩阵 + 重算经济学）调度器的核心语义，防装饰性。

    场景：三重差距（性能/带宽/地域）下，「不可重算大数据的落点」必须被
    传输代价主导，而不是算力主导 —— 这正是 xnu 会翻车、geo 能赢的根因。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "bench_triple_gap_t", os.path.join(ROOT, "bench-triple-gap.py"))
        cls.tg = importlib.util.module_from_spec(spec)
        sys.modules["bench_triple_gap_t"] = cls.tg
        spec.loader.exec_module(cls.tg)

    def _geo(self):
        nodes, region = self.tg.build_triple_nodes()
        cfg = {"nodes": nodes}
        g = self.tg.make_geo_router(cfg)
        g.region = {n["id"]: n.get("region", "site-a") for n in nodes}
        g.bw = {n["id"]: self.tg.BW_MBPS.get(n.get("bwTier", "gbe"), 110.0)
                for n in nodes}
        g.weight = {n["id"]: float(n.get("_weight", 1.0)) for n in nodes}
        return g

    def test_57_geo_avoids_cross_region_for_large_irreducible_data(self):
        # 不可重算的大数据（30GB）其 home 在凤凰城（phx），
        # 凤凰城虽然算力不错（0.5）但搬到别处要付 ~200ms 延迟 + 窄上行。
        # 正确决策：把任务留在 home（phx），而不是甩给算力更高的 core1。
        g = self._geo()
        g.home_map["s0"] = "phx"
        g.size_map["s0"] = 30000.0
        g.recompute_map["s0"] = False
        n = g.pick([nid for nid in g.order], 5000.0, sid="s0", tier="background",
                   home="phx")
        self.assertEqual(n, "phx", "30GB 不可重算数据应留在数据源（搬运代价碾压算力差）")

    def test_58_geo_recomputes_small_data_on_fast_node(self):
        # 可重算的小数据（10MB）其 home 在弱机 edge2（3 代 i5，0.12），
        # 但重算成本远小于「从弱机窄上行搬过来」，正确决策是甩到强机 core1。
        g = self._geo()
        g.home_map["s1"] = "edge2"
        g.size_map["s1"] = 10.0
        g.recompute_map["s1"] = True
        n = g.pick([nid for nid in g.order], 5000.0, sid="s1", tier="default",
                   home="edge2")
        self.assertEqual(n, "core1", "小数据可重算，应甩到最强节点（算力引力主导）")

    def test_59_geo_transfer_matrix_distinguishes_link_quality(self):
        # geo 的传输矩阵能分辨「两条不同质量的链路」（单值 dataMoveCost 做不到）：
        # 同一份 20GB 不可重算数据（home=core1），搬到东京（2G 云链路）应比搬到
        # 凤凰城（1G 云链路）便宜约一半 —— 因为瓶颈带宽 = min(源上行, 目标下行)。
        g = self._geo()
        g.home_map["s2"] = "core1"   # 数据在 site-a 核心（上行 2400MB/s）
        g.size_map["s2"] = 20000.0   # 20GB 不可重算
        g.recompute_map["s2"] = False
        bid_tokyo = g._data_ticks("tokyo", "s2")   # wan-2g，瓶颈 240MB/s
        bid_phx = g._data_ticks("phx", "s2")       # wan-1g，瓶颈 120MB/s
        # 凤凰城上行是东京的一半 → 搬同样数据代价约 2 倍
        self.assertGreater(bid_phx, bid_tokyo * 1.5,
                           "传输矩阵应能分辨窄链路（凤凰城 1G）与宽链路（东京 2G）的代价差")


if __name__ == "__main__":
    unittest.main(verbosity=2)
