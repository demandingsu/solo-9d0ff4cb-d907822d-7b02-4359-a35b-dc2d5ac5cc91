"""异地备份盘点（对账）端到端自测。

覆盖：开单快照固定、分页接收与幂等重试、冲突拒绝、页数齐全才封单、
报告内容、封单后不可变、服务重启续传、并发交错。

运行：pytest -q tests/test_inventory.py
"""

from __future__ import annotations

import os
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def _item(product_id, revision, summary, parent=None):
    payload = {"product_id": product_id, "revision": revision, "summary": summary}
    if parent is not None:
        payload["parent_product_id"] = parent[0]
        payload["parent_revision"] = parent[1]
    return payload


def _entry(product_id, revision, summary):
    return {"product_id": product_id, "revision": revision, "summary": summary}


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app = create_app(db_path)
    with TestClient(app) as c:
        yield c


def _post_batch(client, items):
    return client.post("/api/batches", json={"items": items})


def _open(client, expected_pages, note=None):
    payload = {"expected_pages": expected_pages}
    if note is not None:
        payload["note"] = note
    r = client.post("/api/inventory/orders", json=payload)
    assert r.status_code == 201
    return r.json()


def _upload(client, order_id, page_no, items):
    return client.post(
        f"/api/inventory/orders/{order_id}/pages",
        json={"page_no": page_no, "items": items},
    )


def _seal(client, order_id):
    return client.post(f"/api/inventory/orders/{order_id}/seal")


# ---------------------------------------------------------------------------
# 1. 开单与进度
# ---------------------------------------------------------------------------

def test_open_order_snapshots_active_catalog(client):
    _post_batch(client, [
        _item("R-1", 1, "瓦片v1"),
        _item("R-1", 2, "瓦片v2"),          # 现役为 @2
        _item("M-1", 1, "拼图", parent=("R-1", 2)),
    ])
    order = _open(client, 2, note="2026 春季异地目录")
    assert order["status"] == "open"
    assert order["expected_pages"] == 2
    assert order["snapshot_size"] == 2       # 每个成果号只取现役一条
    assert order["received_pages"] == []
    assert order["missing_pages"] == [1, 2]
    assert order["note"] == "2026 春季异地目录"
    assert order["sealed_at"] is None

    # 列表与详情一致
    listed = client.get("/api/inventory/orders").json()
    assert [o["order_id"] for o in listed] == [order["order_id"]]
    detail = client.get(f"/api/inventory/orders/{order['order_id']}").json()
    assert detail["snapshot_size"] == 2


def test_open_order_validation(client):
    assert client.post(
        "/api/inventory/orders", json={"expected_pages": 0}
    ).status_code == 422
    assert client.get("/api/inventory/orders/999").status_code == 404


# ---------------------------------------------------------------------------
# 2. 分页接收：幂等重试与冲突拒绝
# ---------------------------------------------------------------------------

def test_page_retry_same_content_is_idempotent(client):
    _post_batch(client, [_item("A", 1, "a"), _item("B", 1, "b")])
    order = _open(client, 1)
    oid = order["order_id"]
    page = [_entry("A", 1, "a"), _entry("B", 1, "b")]

    r1 = _upload(client, oid, 1, page)
    assert r1.status_code == 201 and r1.json()["status"] == "received"
    assert r1.json()["received_count"] == 1

    # 同页同内容（条目顺序不同也算同内容）→ 幂等，不重复计数
    r2 = _upload(client, oid, 1, list(reversed(page)))
    assert r2.status_code == 201 and r2.json()["status"] == "duplicate"
    assert r2.json()["received_count"] == 1

    detail = client.get(f"/api/inventory/orders/{oid}").json()
    assert detail["received_pages"] == [1]


def test_page_same_number_different_content_rejected(client):
    _post_batch(client, [_item("A", 1, "a")])
    oid = _open(client, 1)["order_id"]
    assert _upload(client, oid, 1, [_entry("A", 1, "a")]).status_code == 201

    r = _upload(client, oid, 1, [_entry("A", 1, "被改动")])
    assert r.status_code == 409
    # 原页内容保留，封单后报告仍按首次内容对账
    report = _seal(client, oid).json()
    assert report["missing"] == [] and report["mismatched"] == []


def test_cross_page_same_product_rejected(client):
    _post_batch(client, [_item("A", 1, "a"), _item("B", 1, "b")])
    oid = _open(client, 2)["order_id"]
    assert _upload(client, oid, 1, [_entry("A", 1, "a")]).status_code == 201

    # 同号出现在另一页：即使内容相同也拒绝
    r = _upload(client, oid, 2, [_entry("A", 1, "a"), _entry("B", 1, "b")])
    assert r.status_code == 409
    assert "跨页同号" in r.json()["detail"]
    # 被拒页整页不生效：B 也未入库，第 2 页仍缺
    detail = client.get(f"/api/inventory/orders/{oid}").json()
    assert detail["received_pages"] == [1]
    assert detail["missing_pages"] == [2]


def test_page_out_of_range_and_unknown_order(client):
    oid = _open(client, 1)["order_id"]
    assert _upload(client, oid, 2, [_entry("A", 1, "a")]).status_code == 409
    assert _upload(client, oid, 0, [_entry("A", 1, "a")]).status_code == 422
    assert _upload(client, 999, 1, [_entry("A", 1, "a")]).status_code == 404


def test_duplicate_product_within_page_rejected(client):
    oid = _open(client, 1)["order_id"]
    r = _upload(client, oid, 1, [_entry("A", 1, "a"), _entry("A", 1, "a")])
    assert r.status_code == 422


def test_empty_page_allowed(client):
    _post_batch(client, [_item("A", 1, "a")])
    oid = _open(client, 2)["order_id"]
    assert _upload(client, oid, 1, [_entry("A", 1, "a")]).status_code == 201
    assert _upload(client, oid, 2, []).status_code == 201  # 空末页
    report = _seal(client, oid).json()
    assert report["backup_item_count"] == 1
    assert report["missing"] == []


# ---------------------------------------------------------------------------
# 3. 封单门槛与报告内容
# ---------------------------------------------------------------------------

def test_seal_requires_all_pages(client):
    _post_batch(client, [_item("A", 1, "a")])
    oid = _open(client, 2)["order_id"]
    _upload(client, oid, 1, [_entry("A", 1, "a")])

    r = _seal(client, oid)
    assert r.status_code == 409 and "第 2 页" in r.json()["detail"]
    # 封单失败不留半份结果：单仍为 open，报告不可得
    detail = client.get(f"/api/inventory/orders/{oid}").json()
    assert detail["status"] == "open"
    assert client.get(f"/api/inventory/orders/{oid}/report").status_code == 409

    # 补齐后可封单
    _upload(client, oid, 2, [])
    assert _seal(client, oid).status_code == 200


def test_unsealed_order_never_returns_report(client):
    oid = _open(client, 1)["order_id"]
    assert client.get(f"/api/inventory/orders/{oid}/report").status_code == 409
    _upload(client, oid, 1, [])
    # 页齐了但未封单，仍不能取报告
    assert client.get(f"/api/inventory/orders/{oid}/report").status_code == 409


def test_report_missing_mismatch_extra(client):
    _post_batch(client, [
        _item("KEEP", 1, "一致"),
        _item("REV", 2, "本地v2"),          # 备份还是旧修订
        _item("SUM", 1, "本地摘要"),         # 备份摘要被改
        _item("LOST", 1, "备份里没有"),
    ])
    oid = _open(client, 2)["order_id"]
    _upload(client, oid, 1, [
        _entry("KEEP", 1, "一致"),
        _entry("REV", 1, "备份旧版"),
    ])
    _upload(client, oid, 2, [
        _entry("SUM", 1, "备份摘要不同"),
        _entry("EXTRA", 3, "本地没有"),
    ])

    r = _seal(client, oid)
    assert r.status_code == 200
    rep = r.json()
    assert rep["status"] == "sealed"
    assert rep["snapshot_size"] == 4 and rep["backup_item_count"] == 4

    assert [m["product_id"] for m in rep["missing"]] == ["LOST"]
    assert rep["missing"][0]["revision"] == 1

    assert len(rep["mismatched"]) == 2
    mm = {m["product_id"]: m for m in rep["mismatched"]}
    assert mm["REV"]["reasons"] == ["revision", "summary"]
    assert mm["REV"]["local_revision"] == 2
    assert mm["REV"]["backup_revision"] == 1
    assert mm["SUM"]["reasons"] == ["summary"]
    assert mm["SUM"]["local_summary"] == "本地摘要"

    assert [e["product_id"] for e in rep["extra"]] == ["EXTRA"]
    assert rep["extra"][0]["revision"] == 3

    assert rep["missing_count"] == 1
    assert rep["mismatched_count"] == 2
    assert rep["extra_count"] == 1

    # GET 报告与封单返回值一致
    assert client.get(f"/api/inventory/orders/{oid}/report").json() == rep


def test_ingestion_after_open_does_not_change_reconciliation(client):
    _post_batch(client, [_item("R-1", 1, "v1")])
    oid = _open(client, 1)["order_id"]

    # 开单后继续入库：现役升级 + 全新成果
    _post_batch(client, [_item("R-1", 2, "v2"), _item("NEW", 1, "新成果")])

    # 异地目录与开单时的现役一致（R-1@1）
    _upload(client, oid, 1, [_entry("R-1", 1, "v1")])
    rep = _seal(client, oid).json()
    # 对账基准仍是开单快照：R-1@1 不算不符，NEW 不算缺失
    assert rep["missing"] == [] and rep["mismatched"] == []
    assert rep["extra"] == []
    assert rep["snapshot_size"] == 1


def test_sealed_order_is_immutable(client):
    _post_batch(client, [_item("A", 1, "a")])
    oid = _open(client, 1)["order_id"]
    _upload(client, oid, 1, [_entry("A", 1, "a")])
    rep1 = _seal(client, oid).json()

    # 封单后：不能再传页
    assert _upload(client, oid, 1, [_entry("A", 1, "a")]).status_code == 409
    # 重复封单幂等，报告原样
    rep2 = _seal(client, oid).json()
    assert rep2 == rep1
    # 封单后继续入库新修订，已完成报告保持原样
    _post_batch(client, [_item("A", 2, "a-v2"), _item("LATE", 1, "晚到")])
    rep3 = client.get(f"/api/inventory/orders/{oid}/report").json()
    assert rep3 == rep1
    detail = client.get(f"/api/inventory/orders/{oid}").json()
    assert detail["status"] == "sealed" and detail["sealed_at"] is not None


def test_seal_unknown_order(client):
    assert _seal(client, 999).status_code == 404
    assert client.get("/api/inventory/orders/999/report").status_code == 404


# ---------------------------------------------------------------------------
# 4. 服务重启：进度与报告持久化
# ---------------------------------------------------------------------------

def test_restart_preserves_progress_and_blocks_report(tmp_path):
    db_path = str(tmp_path / "restart.db")
    app1 = create_app(db_path)
    with TestClient(app1) as c1:
        _post_batch(c1, [_item("A", 1, "a"), _item("B", 1, "b")])
        oid = _open(c1, 2)["order_id"]
        _upload(c1, oid, 1, [_entry("A", 1, "a")])

    # 模拟重启：同一库文件重新建应用
    app2 = create_app(db_path)
    with TestClient(app2) as c2:
        detail = c2.get(f"/api/inventory/orders/{oid}").json()
        assert detail["status"] == "open"
        assert detail["received_pages"] == [1]
        assert detail["missing_pages"] == [2]
        # 未封单不返回报告
        assert c2.get(f"/api/inventory/orders/{oid}/report").status_code == 409
        # 同页同内容重试在重启后仍幂等
        r = _upload(c2, oid, 1, [_entry("A", 1, "a")])
        assert r.json()["status"] == "duplicate"
        # 续传剩余页并封单
        _upload(c2, oid, 2, [_entry("B", 1, "b")])
        rep = _seal(c2, oid).json()
        assert rep["missing"] == [] and rep["extra"] == []

    # 再次重启：已完成报告保持原样
    app3 = create_app(db_path)
    with TestClient(app3) as c3:
        assert c3.get(f"/api/inventory/orders/{oid}/report").json() == rep


# ---------------------------------------------------------------------------
# 5. 并发交错：传页 / 封单 / 入库 并行
# ---------------------------------------------------------------------------

def test_concurrent_pages_seal_and_ingestion(tmp_path):
    db_path = str(tmp_path / "conc.db")
    app = create_app(db_path)
    pages = 6
    with TestClient(app) as c:
        _post_batch(c, [_item(f"P{i}", 1, f"v{i}") for i in range(pages)])
        oid = _open(c, pages)["order_id"]

    errors: list[Exception] = []
    seal_results: list[int] = []

    def uploader(page_no: int):
        try:
            with TestClient(app) as c:
                r = _upload(c, oid, page_no, [_entry(f"P{page_no-1}", 1, f"v{page_no-1}")])
                assert r.status_code == 201
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def ingester():
        try:
            with TestClient(app) as c:
                # 开单后入库的新修订不应影响对账
                _post_batch(c, [_item("P0", 2, "v0-new"), _item("LATE", 1, "晚到")])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def sealer():
        try:
            with TestClient(app) as c:
                r = _seal(c, oid)
                seal_results.append(r.status_code)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=uploader, args=(p,)) for p in range(1, pages + 1)]
    threads.append(threading.Thread(target=ingester))
    threads.append(threading.Thread(target=sealer))  # 提前尝试封单：应 409 或碰巧最后成功
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    with TestClient(app) as c:
        detail = c.get(f"/api/inventory/orders/{oid}").json()
        assert detail["received_pages"] == list(range(1, pages + 1))
        # 提前的封单若成功则报告已可用，否则现在封单必须成功
        rep = _seal(c, oid).json()
        assert rep["status"] == "sealed"
        # 对账基准为开单快照：P0@1 与备份一致，LATE 不在快照中
        assert rep["missing"] == [] and rep["mismatched"] == []
        assert rep["extra"] == []
        assert rep["snapshot_size"] == pages
        # 封单结果唯一确定：不论交错顺序如何，最终报告一致
        again = c.get(f"/api/inventory/orders/{oid}/report").json()
        assert again == rep
        assert all(code in (200, 409) for code in seal_results)


def test_concurrent_same_page_conflict_is_serialized(tmp_path):
    # 两个线程同时上传同一页、内容不同：恰有一个成功，另一个 409，无脏数据
    db_path = str(tmp_path / "conflict.db")
    app = create_app(db_path)
    with TestClient(app) as c:
        _post_batch(c, [_item("A", 1, "a")])
        oid = _open(c, 1)["order_id"]

    outcomes: list[tuple[int, str]] = []

    def worker(summary: str):
        with TestClient(app) as c:
            r = _upload(c, oid, 1, [_entry("A", 1, summary)])
            outcomes.append((r.status_code, summary))

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("a-改动",))
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(code for code, _ in outcomes)
    assert codes == [201, 409]
    with TestClient(app) as c:
        rep = _seal(c, oid).json()
        # 先到的内容胜出；若 "a" 胜出则完全吻合，否则记为摘要不符——
        # 无论哪种都必须自洽，不允许出现半页写入
        if rep["mismatched"]:
            assert rep["mismatched"][0]["reasons"] == ["summary"]
            assert rep["mismatched"][0]["backup_summary"] == "a-改动"
        else:
            assert rep["missing"] == [] and rep["extra"] == []
