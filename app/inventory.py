"""异地备份盘点（对账）：开单快照、分页接收、封单出报告。

一致性设计：
- 开单（create_order）在单个写事务内写入盘点单并复制本地现役目录快照，
  开单之后再入库的新修订不影响本次对账。
- 页上传（submit_page）在单个 BEGIN IMMEDIATE 事务内完成全部校验与写入：
  同页同内容重试幂等返回；同页换内容、跨页同号、页号越界、已封单均拒绝，
  拒绝时不产生任何写入。
- 封单（seal_order）在单个写事务内校验页数齐全、计算报告、写入报告并
  置状态为 sealed——要么全部成功，要么整体回滚，不会留下半份结果；
  重复封单幂等返回已存报告，已完成报告保持原样。
- 所有状态都持久化在 SQLite 中，服务重启后未封单可继续传页，
  但未封单一律不返回报告（GET report → 409）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Optional

from . import database as db


class InventoryError(Exception):
    """盘点业务错误，由路由层翻译为 HTTP 响应。"""

    def __init__(self, detail: str, *, status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _not_found(order_id: int) -> InventoryError:
    return InventoryError(f"盘点单 {order_id} 不存在", status_code=404)


def _rollback(conn: sqlite3.Connection) -> None:
    """仅在有活动事务时回滚（某些路径在 COMMIT 之后才报错）。"""
    if conn.in_transaction:
        conn.execute("ROLLBACK")


# ---------------------------------------------------------------------------
# 读取辅助（须在已开启的事务/快照内调用）
# ---------------------------------------------------------------------------

def get_order(conn: sqlite3.Connection, order_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM inventory_orders WHERE order_id = ?", (order_id,)
    ).fetchone()


def list_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM inventory_orders ORDER BY order_id")
    )


def _received_pages(conn: sqlite3.Connection, order_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT page_no FROM inventory_pages WHERE order_id = ? ORDER BY page_no",
        (order_id,),
    ).fetchall()
    return [int(r["page_no"]) for r in rows]


def order_record(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """组装单详情（含分页进度）；须在事务内调用。"""
    received = _received_pages(conn, row["order_id"])
    received_set = set(received)
    missing = [p for p in range(1, row["expected_pages"] + 1)
               if p not in received_set]
    return {
        "order_id": row["order_id"],
        "status": row["status"],
        "expected_pages": row["expected_pages"],
        "received_pages": received,
        "missing_pages": missing,
        "snapshot_size": row["snapshot_size"],
        "note": row["note"],
        "created_at": row["created_at"],
        "sealed_at": row["sealed_at"],
    }


# ---------------------------------------------------------------------------
# 开单：固定本地现役目录快照 + 预定页数
# ---------------------------------------------------------------------------

def create_order(
    conn: sqlite3.Connection, expected_pages: int, note: Optional[str] = None
) -> dict[str, Any]:
    """开单：同一事务内建单并快照当前全部本地现役修订。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = db.utc_now_iso()
        cur = conn.execute(
            "INSERT INTO inventory_orders (status, expected_pages, note, created_at) "
            "VALUES ('open', ?, ?, ?)",
            (expected_pages, note, now),
        )
        order_id = int(cur.lastrowid)
        active = db.list_active_products(conn)
        conn.executemany(
            "INSERT INTO inventory_snapshots (order_id, product_id, revision, summary) "
            "VALUES (?, ?, ?, ?)",
            [
                (order_id, row["product_id"], row["revision"], row["summary"])
                for row in active
            ],
        )
        conn.execute(
            "UPDATE inventory_orders SET snapshot_size = ? WHERE order_id = ?",
            (len(active), order_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise

    return {
        "order_id": order_id,
        "status": "open",
        "expected_pages": expected_pages,
        "received_pages": [],
        "missing_pages": list(range(1, expected_pages + 1)),
        "snapshot_size": len(active),
        "note": note,
        "created_at": now,
        "sealed_at": None,
    }


# ---------------------------------------------------------------------------
# 分页接收
# ---------------------------------------------------------------------------

def _page_content_hash(items: list[dict[str, Any]]) -> str:
    """页内容的规范化哈希：按成果号排序后序列化，与条目上传顺序无关。"""
    canonical = sorted(
        (it["product_id"], it["revision"], it["summary"]) for it in items
    )
    blob = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def submit_page(
    conn: sqlite3.Connection,
    order_id: int,
    page_no: int,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """接收一页异地目录。

    - 同页同内容（顺序无关）重试：幂等返回 duplicate，不重复写入；
    - 同页换内容、跨页同号、页号越界、单已封：拒绝，零写入。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        order = get_order(conn, order_id)
        if order is None:
            raise _not_found(order_id)
        if order["status"] == "sealed":
            raise InventoryError(
                f"盘点单 {order_id} 已封单，报告已定稿，不能再上传页"
            )
        if not 1 <= page_no <= order["expected_pages"]:
            raise InventoryError(
                f"页号 {page_no} 超出范围：本单预定页数为 "
                f"{order['expected_pages']}（合法页号 1..{order['expected_pages']}）"
            )

        content_hash = _page_content_hash(items)
        existing = conn.execute(
            "SELECT * FROM inventory_pages WHERE order_id = ? AND page_no = ?",
            (order_id, page_no),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] == content_hash:
                # 同页同内容重试：幂等，不产生新写入
                received_count = len(_received_pages(conn, order_id))
                conn.execute("COMMIT")
                return {
                    "order_id": order_id,
                    "page_no": page_no,
                    "status": "duplicate",
                    "item_count": existing["item_count"],
                    "received_count": received_count,
                    "expected_pages": order["expected_pages"],
                    "message": "该页与已接收内容完全一致，按重复上传处理（幂等）",
                }
            raise InventoryError(
                f"第 {page_no} 页已接收，本次上传内容与其不一致，"
                f"已拒绝并保留原页内容"
            )

        # 跨页同号冲突：每个成果号在整单中只允许出现一次
        if items:
            product_ids = [it["product_id"] for it in items]
            placeholders = ",".join("?" for _ in product_ids)
            clashes = conn.execute(
                f"SELECT DISTINCT product_id, page_no FROM inventory_page_items "
                f"WHERE order_id = ? AND product_id IN ({placeholders}) "
                f"ORDER BY product_id",
                (order_id, *product_ids),
            ).fetchall()
            if clashes:
                detail = "；".join(
                    f"成果 {c['product_id']} 已出现在第 {c['page_no']} 页"
                    for c in clashes
                )
                raise InventoryError(
                    f"跨页同号冲突：{detail}；每个成果号仅允许报一个档案修订，"
                    f"本页已整页拒收"
                )

        now = db.utc_now_iso()
        conn.execute(
            "INSERT INTO inventory_pages "
            "(order_id, page_no, item_count, content_hash, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (order_id, page_no, len(items), content_hash, now),
        )
        conn.executemany(
            "INSERT INTO inventory_page_items "
            "(order_id, page_no, product_id, revision, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (order_id, page_no, it["product_id"], it["revision"], it["summary"])
                for it in items
            ],
        )
        received_count = len(_received_pages(conn, order_id))
        conn.execute("COMMIT")
        return {
            "order_id": order_id,
            "page_no": page_no,
            "status": "received",
            "item_count": len(items),
            "received_count": received_count,
            "expected_pages": order["expected_pages"],
            "message": "页已接收",
        }
    except InventoryError:
        _rollback(conn)
        raise
    except Exception:
        _rollback(conn)
        raise


# ---------------------------------------------------------------------------
# 封单与报告
# ---------------------------------------------------------------------------

def _assemble_report(
    order: sqlite3.Row,
    missing: list[dict[str, Any]],
    mismatched: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    backup_item_count: int,
) -> dict[str, Any]:
    return {
        "order_id": order["order_id"],
        "status": "sealed",
        "expected_pages": order["expected_pages"],
        "snapshot_size": order["snapshot_size"],
        "backup_item_count": backup_item_count,
        "missing_count": len(missing),
        "mismatched_count": len(mismatched),
        "extra_count": len(extra),
        "created_at": order["created_at"],
        "sealed_at": order["sealed_at"],
        "missing": missing,
        "mismatched": mismatched,
        "extra": extra,
    }


def _read_stored_report(
    conn: sqlite3.Connection, order: sqlite3.Row
) -> Optional[dict[str, Any]]:
    rep = conn.execute(
        "SELECT * FROM inventory_reports WHERE order_id = ?",
        (order["order_id"],),
    ).fetchone()
    if rep is None:
        return None
    return _assemble_report(
        order,
        json.loads(rep["missing_json"]),
        json.loads(rep["mismatched_json"]),
        json.loads(rep["extra_json"]),
        rep["backup_item_count"],
    )


def _compute_report(
    conn: sqlite3.Connection, order_id: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    """按开单快照与已收目录页比对：缺失 / 修订或摘要不符 / 备份多出。"""
    snapshot = conn.execute(
        "SELECT * FROM inventory_snapshots WHERE order_id = ? ORDER BY product_id",
        (order_id,),
    ).fetchall()
    backup_rows = conn.execute(
        "SELECT * FROM inventory_page_items WHERE order_id = ? ORDER BY product_id",
        (order_id,),
    ).fetchall()
    backup_by_pid = {r["product_id"]: r for r in backup_rows}

    missing: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    local_pids: set[str] = set()
    for s in snapshot:
        pid = s["product_id"]
        local_pids.add(pid)
        b = backup_by_pid.get(pid)
        if b is None:
            missing.append({
                "product_id": pid,
                "revision": s["revision"],
                "summary": s["summary"],
            })
            continue
        reasons: list[str] = []
        if b["revision"] != s["revision"]:
            reasons.append("revision")
        if b["summary"] != s["summary"]:
            reasons.append("summary")
        if reasons:
            mismatched.append({
                "product_id": pid,
                "local_revision": s["revision"],
                "local_summary": s["summary"],
                "backup_revision": b["revision"],
                "backup_summary": b["summary"],
                "reasons": reasons,
            })

    extra = [
        {
            "product_id": r["product_id"],
            "revision": r["revision"],
            "summary": r["summary"],
        }
        for r in backup_rows
        if r["product_id"] not in local_pids
    ]
    return missing, mismatched, extra, len(backup_rows)


def seal_order(conn: sqlite3.Connection, order_id: int) -> dict[str, Any]:
    """封单：页数齐全才结算；报告与状态同事务原子落库；重复封单幂等。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        order = get_order(conn, order_id)
        if order is None:
            raise _not_found(order_id)
        if order["status"] == "sealed":
            # 幂等：返回已存报告，已完成报告保持原样
            report = _read_stored_report(conn, order)
            conn.execute("COMMIT")
            if report is None:  # 状态与报告同事务写入，理论上不可达
                raise InventoryError(
                    f"盘点单 {order_id} 状态异常：已封单但报告缺失",
                    status_code=500,
                )
            return report

        received = set(_received_pages(conn, order_id))
        missing_pages = [p for p in range(1, order["expected_pages"] + 1)
                         if p not in received]
        if missing_pages:
            raise InventoryError(
                f"页数不齐全，不能封单：尚缺第 "
                f"{', '.join(map(str, missing_pages))} 页"
                f"（已收 {len(received)}/{order['expected_pages']} 页）"
            )

        missing, mismatched, extra, backup_count = _compute_report(conn, order_id)
        now = db.utc_now_iso()
        conn.execute(
            "INSERT INTO inventory_reports "
            "(order_id, missing_json, mismatched_json, extra_json, "
            " backup_item_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                order_id,
                json.dumps(missing, ensure_ascii=False),
                json.dumps(mismatched, ensure_ascii=False),
                json.dumps(extra, ensure_ascii=False),
                backup_count,
                now,
            ),
        )
        conn.execute(
            "UPDATE inventory_orders SET status = 'sealed', sealed_at = ? "
            "WHERE order_id = ?",
            (now, order_id),
        )
        conn.execute("COMMIT")

        sealed_order = dict(order)
        sealed_order["status"] = "sealed"
        sealed_order["sealed_at"] = now
        return _assemble_report(
            sealed_order, missing, mismatched, extra, backup_count
        )
    except InventoryError:
        _rollback(conn)
        raise
    except Exception:
        _rollback(conn)
        raise


def get_report(conn: sqlite3.Connection, order_id: int) -> dict[str, Any]:
    """读取已定稿报告；未封单一律不返回（调用方须持快照事务）。"""
    order = get_order(conn, order_id)
    if order is None:
        raise _not_found(order_id)
    if order["status"] != "sealed":
        received = len(_received_pages(conn, order_id))
        raise InventoryError(
            f"盘点单 {order_id} 尚未封单（已收 {received}/"
            f"{order['expected_pages']} 页），不提供对账报告"
        )
    report = _read_stored_report(conn, order)
    if report is None:  # 状态与报告同事务写入，理论上不可达
        raise InventoryError(
            f"盘点单 {order_id} 状态异常：已封单但报告缺失", status_code=500
        )
    return report
