"""迟到补入：红测先行，再落写路径。

夹具复现假绿：空桩承诺 ok:true，但桶行仍在、正式无键（双空），
或桶行与正式事件同时持键（双影）——互证页与迟到合计撕裂。
口径择一：甲路径（整笔同事务），写入迟到补入册，当天禁止改口。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import db as dbmod
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "late.db")
    init_db()
    yield


def _auth(c: TestClient, u: str = "admin", p: str = "123456") -> dict:
    tok = c.post("/api/login", json={"username": u, "password": p}).json()["token"]
    return {"Authorization": "Bearer " + tok}


@pytest.fixture
def scenario():
    """补入场景夹具：源窗关闭后事件入迟到桶；另开目标窗覆盖事件时间。"""
    c = TestClient(app)
    h = _auth(c)
    c.post("/api/windows/2/close", headers=h)
    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "LATE-P1",
            "event_ts": "2026-11-02T10:00:00",
            "amount": 7.5,
        },
    )
    assert r.json()["disposition"] == "迟到桶"
    late_id = [
        x
        for x in c.get("/api/late-bucket", headers=h).json()["items"]
        if x["biz_key"] == "LATE-P1"
    ][0]["id"]
    c.post(
        "/api/windows",
        headers=h,
        json={
            "name": "W-1102B",
            "open_at": "2026-11-02T00:00:00",
            "close_at": "2026-11-02T23:59:59",
        },
    )
    wid = [
        w
        for w in c.get("/api/windows", headers=h).json()["items"]
        if w["name"] == "W-1102B"
    ][0]["id"]
    return {
        "client": c,
        "headers": h,
        "late_id": late_id,
        "target": "W-1102B",
        "target_wid": wid,
        "biz_key": "LATE-P1",
    }


def five_places(c: TestClient, h: dict, biz_key: str, target_wid: int) -> dict:
    """五处同态取样：迟到桶 / 正式事件 / 目标窗详情 / 对账互证 / 审计。"""
    late = c.get("/api/late-bucket", headers=h).json()["items"]
    events = c.get("/api/events", headers=h).json()["items"]
    detail = c.get(f"/api/windows/{target_wid}", headers=h).json()
    recon = c.get(f"/api/reconcile/{target_wid}", headers=h).json()
    audit = c.get("/api/audit", headers=h).json()["items"]
    ledger = c.get("/api/late-promote-ledger", headers=h).json()
    return {
        "bucket_rows": [x for x in late if x["biz_key"] == biz_key],
        "formal_rows": [
            x
            for x in events
            if x["biz_key"] == biz_key and x["disposition"] == "窗内"
        ],
        "window_detail_count": detail["in_window_count"],
        "reconcile_count": recon["window_events"],
        "promote_audits": [
            a for a in audit if a["action"] in ("late_promote", "补入偏了")
        ],
        "ledger_rows": [
            x for x in ledger.get("items", []) if x["biz_key"] == biz_key
        ],
    }


def assert_exactly_one_place(places: dict, expect_count: int) -> None:
    """桶与正式仅一处有该键；目标窗详情与对账互证同数。"""
    assert places["bucket_rows"] == [], "迟到桶仍持键（双影）"
    assert len(places["formal_rows"]) == 1, "正式窗内事件须恰一笔（双空/双影）"
    assert places["window_detail_count"] == expect_count, "目标窗详情未同态"
    assert places["reconcile_count"] == expect_count, "对账互证未同态"


def test_promote_leaves_key_in_exactly_one_place(scenario):
    """红测①：补入后双影/双空皆判负——五处同态，键仅一处。"""
    c, h = scenario["client"], scenario["headers"]
    wid = scenario["target_wid"]
    before = five_places(c, h, scenario["biz_key"], wid)
    assert len(before["bucket_rows"]) == 1 and before["formal_rows"] == []

    r = c.post(
        "/api/late-bucket/promote",
        headers=h,
        json={"late_id": scenario["late_id"], "target_window_name": scenario["target"]},
    )
    assert r.status_code == 200, r.text
    assert not r.json().get("stub"), "补入仍是空桩：假绿"

    places = five_places(c, h, scenario["biz_key"], wid)
    assert_exactly_one_place(places, before["window_detail_count"] + 1)
    assert len(places["ledger_rows"]) == 1, "补入册须逐笔可核证"
    assert any(a["action"] == "late_promote" for a in places["promote_audits"])


def test_concurrent_promote_same_late_id_contends(scenario):
    """红测②：两人同时补同一迟到行——补入锁只有一人占，后到只记争用。"""
    c, h1 = scenario["client"], scenario["headers"]
    h2 = _auth(c, "closer", "close123")
    lock_key = f"late-promote:{scenario['late_id']}"
    body = {"late_id": scenario["late_id"], "target_window_name": scenario["target"]}

    # 甲先占补入锁（模拟在途补入），乙后到：409 且只记争用，业务零改
    assert c.post("/api/locks/acquire", headers=h1, json={"lock_key": lock_key}).status_code == 200
    r2 = c.post("/api/late-bucket/promote", headers=h2, json=body)
    assert r2.status_code == 409, r2.text
    cont = c.get("/api/contention", headers=h1).json()["items"]
    assert any(x["lock_key"] == lock_key for x in cont), "后到须只记争用"
    mid = five_places(c, h1, scenario["biz_key"], scenario["target_wid"])
    assert len(mid["bucket_rows"]) == 1 and mid["formal_rows"] == [], "争用方不得写业务"

    # 甲释锁后补入落成；乙再补：迟到行已不在，正式仍恰一笔
    c.post("/api/locks/release", headers=h1, json={"lock_key": lock_key})
    r1 = c.post("/api/late-bucket/promote", headers=h1, json=body)
    assert r1.status_code == 200 and not r1.json().get("stub"), r1.text
    r3 = c.post("/api/late-bucket/promote", headers=h2, json=body)
    assert r3.status_code in (404, 409), r3.text
    places = five_places(c, h1, scenario["biz_key"], scenario["target_wid"])
    assert_exactly_one_place(places, 1)
    assert len(places["ledger_rows"]) == 1, "同一迟到行只准落成一笔"


def test_torn_fixture_reproduces_false_green(scenario):
    """夹具复现假绿：直造双影（桶+正式同键）与双空（两侧皆无），校验器须报警。"""
    c, h = scenario["client"], scenario["headers"]
    wid = scenario["target_wid"]
    with dbmod.connect() as conn:
        cur = conn.execute(
            """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
               VALUES (?,?,?,?,?,?)""",
            (wid, "TORN-DOUBLE", "2026-11-02T10:00:00", 1.0, "窗内", "2026-11-02T10:00:01"),
        )
        conn.execute(
            """INSERT INTO late_bucket(event_id,window_name,biz_key,event_ts,amount,reason,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (cur.lastrowid, "W-1102B", "TORN-DOUBLE", "2026-11-02T10:00:00", 1.0, "关窗后到达", "2026-11-02T10:00:02"),
        )
        conn.commit()
    double = five_places(c, h, "TORN-DOUBLE", wid)
    with pytest.raises(AssertionError):
        assert_exactly_one_place(double, 1)  # 双影：桶与正式同键
    empty = five_places(c, h, "TORN-NONE", wid)
    with pytest.raises(AssertionError):
        assert_exactly_one_place(empty, 1)  # 双空：两侧皆无此键


def test_closed_target_refuses_and_keeps_both_sides(scenario):
    """目标窗已关闭：拒写，正式零增、桶行保留。"""
    c, h = scenario["client"], scenario["headers"]
    c.post(f"/api/windows/{scenario['target_wid']}/close", headers=h)
    r = c.post(
        "/api/late-bucket/promote",
        headers=h,
        json={"late_id": scenario["late_id"], "target_window_name": scenario["target"]},
    )
    assert r.status_code == 409, r.text
    places = five_places(c, h, scenario["biz_key"], scenario["target_wid"])
    assert len(places["bucket_rows"]) == 1, "桶行须保留"
    assert places["formal_rows"] == [], "正式零增"
    assert places["ledger_rows"] == []


def test_commit_failure_rolls_back_both_sides(scenario):
    """同事务提交失败：两侧回滚，正式零增、桶行保留、册无此笔。"""
    c, h = scenario["client"], scenario["headers"]
    r = c.post(
        "/api/late-bucket/promote",
        headers=h,
        json={
            "late_id": scenario["late_id"],
            "target_window_name": scenario["target"],
            "fail_simulate": True,
        },
    )
    assert r.status_code == 500, r.text
    places = five_places(c, h, scenario["biz_key"], scenario["target_wid"])
    assert len(places["bucket_rows"]) == 1, "桶行须保留"
    assert places["formal_rows"] == [], "正式零增"
    assert places["ledger_rows"] == [], "补入册不得有作废笔"
    audits = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["action"] == "late_promote_rollback" for a in audits)


def test_register_same_day_no_flip(scenario):
    """迟到补入册：当天禁止改口，改口拒写并记「补入偏了」。"""
    c, h = scenario["client"], scenario["headers"]
    cur = c.get("/api/late-promote-register", headers=h).json()
    assert cur["register"]["path"] == "甲"
    assert cur["implemented_path"] == "甲"
    r = c.post("/api/late-promote-register", headers=h, json={"path": "乙"})
    assert r.status_code == 409, r.text
    again = c.post("/api/late-promote-register", headers=h, json={"path": "甲"})
    assert again.status_code == 200 and again.json()["already"] is True
    audits = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["action"] == "补入偏了" for a in audits)


def test_register_deviation_refuses_promote(scenario):
    """实现口径与册不符：拒写并记「补入偏了」，业务零改。"""
    c, h = scenario["client"], scenario["headers"]
    with dbmod.connect() as conn:
        conn.execute("UPDATE promote_register SET path='乙'")
        conn.commit()
    r = c.post(
        "/api/late-bucket/promote",
        headers=h,
        json={"late_id": scenario["late_id"], "target_window_name": scenario["target"]},
    )
    assert r.status_code == 409 and "补入偏了" in r.json()["detail"], r.text
    places = five_places(c, h, scenario["biz_key"], scenario["target_wid"])
    assert len(places["bucket_rows"]) == 1 and places["formal_rows"] == []
    assert any(a["action"] == "补入偏了" for a in places["promote_audits"])


def test_reconcile_aligns_after_promote(scenario):
    """全绿收口：补入后关窗物化，对账可对齐且互证含补入落成数。"""
    c, h = scenario["client"], scenario["headers"]
    wid = scenario["target_wid"]
    r = c.post(
        "/api/late-bucket/promote",
        headers=h,
        json={"late_id": scenario["late_id"], "target_window_name": scenario["target"]},
    )
    assert r.status_code == 200, r.text
    c.post(f"/api/windows/{wid}/close", headers=h)
    m = c.post(f"/api/windows/{wid}/materialize", headers=h)
    assert m.status_code == 200, m.text
    recon = c.get(f"/api/reconcile/{wid}", headers=h).json()
    assert recon["aligned"] is True, "对账须可对齐"
    assert recon["window_events"] == 1
    assert recon["promoted_in"] == 1, "互证须含补入落成数"
    summary = c.get("/api/late-bucket/summary", headers=h).json()
    assert all(x["window_name"] != "W-1102" for x in summary["by_window"])
