"""迟到桶：补入正式落地（甲路径·整笔同事务）+ 迟到补入册。

口径择一不混用：删迟到桶行与插正式窗内事件同库事务提交；
提交失败两侧回滚，正式零增、桶行保留。无 promote_draft，不走乙路径。
偏离迟到补入册：拒写并记「补入偏了」。同一 late_id 并发补入占锁，后到只记争用。
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import connect, write_audit
from locks import release, try_acquire

router = APIRouter(tags=["late"])

IMPLEMENTED_PATH = "甲"  # 整笔同事务：本仓迟到补入唯一写路径
PATH_LABELS = {"甲": "整笔同事务", "乙": "影子草稿原子切换"}


class LatePromoteIn(BaseModel):
    late_id: int
    target_window_name: str
    fail_simulate: bool = False  # 夹具用：模拟提交失败，验证两侧回滚


class RegisterIn(BaseModel):
    path: str


class _SimulatedCommitFailure(Exception):
    """夹具用：同事务提交前模拟失败。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _current_register(conn):
    return conn.execute(
        "SELECT * FROM promote_register ORDER BY id DESC LIMIT 1"
    ).fetchone()


@router.get("/api/late-bucket")
def list_late(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM late_bucket ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.get("/api/late-bucket/summary")
def late_summary(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM late_bucket"
        ).fetchone()
        by_window = conn.execute(
            """SELECT window_name, COUNT(*) c, COALESCE(SUM(amount),0) s
               FROM late_bucket GROUP BY window_name ORDER BY window_name"""
        ).fetchall()
        return {
            "count": row["c"],
            "amount": row["s"],
            "by_window": [dict(r) for r in by_window],
        }


@router.get("/api/late-promote-register")
def get_register(authorization: AuthHeader = None):
    """迟到补入册：当前口径（最新一行）。"""
    require_user(authorization)
    with connect() as conn:
        row = _current_register(conn)
        return {
            "register": dict(row) if row else None,
            "implemented_path": IMPLEMENTED_PATH,
            "paths": PATH_LABELS,
        }


@router.post("/api/late-promote-register")
def decide_register(body: RegisterIn, authorization: AuthHeader = None):
    """登记当日口径。当天禁止改口：当日改口径拒写并记「补入偏了」。"""
    user = require_user(authorization)
    if body.path not in PATH_LABELS:
        raise HTTPException(422, "口径只认甲（整笔同事务）或乙（影子草稿原子切换）")
    today = date.today().isoformat()
    with connect() as conn:
        cur = _current_register(conn)
        if cur and cur["decided_on"] == today:
            if cur["path"] == body.path:
                return {"ok": True, "already": True, "register": dict(cur)}
            write_audit(
                conn,
                user,
                "补入偏了",
                "promote_register",
                {"held": cur["path"], "asked": body.path, "reason": "当天禁止改口"},
            )
            conn.commit()
            raise HTTPException(409, f"当天禁止改口：迟到补入册今日已定{cur['path']}路径")
        conn.execute(
            "INSERT INTO promote_register(path,decided_by,decided_on,created_at) VALUES (?,?,?,?)",
            (body.path, user, today, _now()),
        )
        write_audit(
            conn, user, "late_promote_path_register", body.path, {"decided_on": today}
        )
        conn.commit()
        row = _current_register(conn)
        return {"ok": True, "register": dict(row)}


@router.get("/api/late-promote-ledger")
def list_promote_ledger(authorization: AuthHeader = None):
    """补入册逐笔：每次落成可核证。"""
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM promote_ledger ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/late-bucket/promote")
def promote(body: LatePromoteIn, authorization: AuthHeader = None):
    """甲路径·整笔同事务：删桶行 + 插正式窗内事件，同库事务提交。

    失败则两侧回滚：正式零增、桶行保留。目标窗须开启且事件时间在窗内。
    """
    user = require_user(authorization)
    lock_key = f"late-promote:{body.late_id}"
    with connect() as conn:
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 落争用记录：后到只记争用，不写业务
            raise HTTPException(409, "争用：同一迟到行他人在补，后到只记争用")
        try:
            reg = _current_register(conn)
            if not reg:
                raise HTTPException(409, "迟到补入册未定口径：请先登记")
            if reg["path"] != IMPLEMENTED_PATH:
                write_audit(
                    conn,
                    user,
                    "补入偏了",
                    str(body.late_id),
                    {
                        "register_path": reg["path"],
                        "implemented_path": IMPLEMENTED_PATH,
                        "reason": "实现口径与迟到补入册不符，拒写",
                    },
                )
                conn.commit()
                raise HTTPException(409, "补入偏了：实现口径与迟到补入册不符，拒写")
            late = conn.execute(
                "SELECT * FROM late_bucket WHERE id=?", (body.late_id,)
            ).fetchone()
            if not late:
                raise HTTPException(404, "迟到记录不存在")
            w = conn.execute(
                "SELECT * FROM windows WHERE name=?", (body.target_window_name,)
            ).fetchone()
            if not w:
                raise HTTPException(404, "目标窗口不存在")
            if w["status"] != "开启":
                write_audit(
                    conn,
                    user,
                    "late_promote_refused",
                    str(body.late_id),
                    {"reason": "目标窗未开启", "target": w["name"]},
                )
                conn.commit()
                raise HTTPException(409, "目标窗未开启，禁止补入")
            if not (w["open_at"] <= late["event_ts"] <= w["close_at"]):
                write_audit(
                    conn,
                    user,
                    "late_promote_refused",
                    str(body.late_id),
                    {"reason": "事件时间不在目标窗范围", "target": w["name"]},
                )
                conn.commit()
                raise HTTPException(409, "事件时间不在目标窗范围内，禁止补入")
            now = _now()
            try:
                # 整笔同事务：旧影标记已补入 + 插正式窗内事件 + 删桶行 + 补入册逐笔
                conn.execute(
                    "UPDATE events SET disposition='已补入' WHERE id=? AND disposition='迟到桶'",
                    (late["event_id"],),
                )
                cur = conn.execute(
                    """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (w["id"], late["biz_key"], late["event_ts"], late["amount"], "窗内", now),
                )
                new_eid = cur.lastrowid
                conn.execute("DELETE FROM late_bucket WHERE id=?", (body.late_id,))
                conn.execute(
                    """INSERT INTO promote_ledger(late_id,biz_key,source_window,target_window_id,
                       target_window_name,event_id,actor,path,status,detail,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        body.late_id,
                        late["biz_key"],
                        late["window_name"],
                        w["id"],
                        w["name"],
                        new_eid,
                        user,
                        IMPLEMENTED_PATH,
                        "已落成",
                        "甲路径·整笔同事务",
                        now,
                    ),
                )
                write_audit(
                    conn,
                    user,
                    "late_promote",
                    str(body.late_id),
                    {
                        "biz_key": late["biz_key"],
                        "target_window": w["name"],
                        "event_id": new_eid,
                        "path": IMPLEMENTED_PATH,
                    },
                )
                if body.fail_simulate:
                    raise _SimulatedCommitFailure("模拟补入提交失败（夹具用）")
                conn.commit()
            except Exception:
                conn.rollback()  # 提交失败：两侧回滚，正式零增、桶行保留
                write_audit(
                    conn,
                    user,
                    "late_promote_rollback",
                    str(body.late_id),
                    {"biz_key": late["biz_key"], "target_window": w["name"]},
                )
                conn.commit()
                raise HTTPException(500, "补入提交失败已回滚：正式零增、桶行保留")
            return {
                "ok": True,
                "stub": False,
                "path": IMPLEMENTED_PATH,
                "late_id": body.late_id,
                "event_id": new_eid,
                "window_id": w["id"],
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/late-bucket/{late_id}/discard-stub")
def discard_stub(late_id: int, authorization: AuthHeader = None):
    """空桩：作废迟到项（不删物理行，只审计）。"""
    user = require_user(authorization)
    with connect() as conn:
        late = conn.execute(
            "SELECT * FROM late_bucket WHERE id=?", (late_id,)
        ).fetchone()
        if not late:
            raise HTTPException(404, "迟到记录不存在")
        write_audit(
            conn,
            user,
            "late_discard_stub",
            str(late_id),
            {"biz_key": late["biz_key"], "at": _now()},
        )
        conn.commit()
    return {"ok": True, "stub": True}
