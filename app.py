"""公开现场抽签服务 (Flask + SQLite).

流程:
  1) 浏览器端生成随机种子, 仅把种子的 SHA-256 承诺与中奖名额提交到服务端;
  2) 按唯一编号登记参与者, 封存前可反复修改名单;
  3) 封存时校验人数 >= 名额, 固化"按编号排序的规范名单快照"及其摘要, 此后名单不可改;
  4) 揭晓时提交种子: 承诺不匹配 -> 保持封存; 匹配 -> 按 RULE_ID 公开规则确定性选出
     不重复中奖编号, 结果与状态一次性写入; 重复揭晓不会重抽;
  5) 导出含种子/名单/摘要/规则/结果的核验包, 任何人可用独立核验器重算比对。
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Flask, Response, g, jsonify, render_template, request

RULE_ID = "pick-sha256-v1"
RULE_VERSION = 1

# 规范规则(同时写入数据库快照、核验包与 README, 任何实现都必须照此计算)
RULE_DESCRIPTION = (
    "对规范名单(按编号数值升序, 每行 '编号<TAB>备注')中的每个编号 id, "
    "计算 score = lowercase(hex(SHA-256(UTF8(seed + ':' + str(id))))); "
    "按 (score 升序, 编号升序) 排序, 前 winner_count 个不重复编号即为中奖者。"
)

MAX_PARTICIPANTS = 100_000
MAX_LABEL_LEN = 100
HEX_RE = re.compile(r"^[0-9a-fA-F]{32,128}$")
ID_MIN, ID_MAX = 1, 10**12

DB_PATH = os.environ.get("DRAW_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "draw.db"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 单次名单提交上限 16MB


# --------------------------------------------------------------------------- 基础工具

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class _ConflictOrRevealed(Exception):
    def __init__(self, rid):
        super().__init__("conflict")
        self.rid = rid


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify({"ok": False, "error": err.message}), err.status


@app.errorhandler(404)
def handle_404(_):
    return jsonify({"ok": False, "error": "not found"}), 404


@app.errorhandler(405)
def handle_405(_):
    return jsonify({"ok": False, "error": "method not allowed"}), 405


@app.errorhandler(413)
def handle_413(_):
    return jsonify({"ok": False, "error": "request body too large"}), 413


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        init_db(conn)
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rounds (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            status            TEXT NOT NULL DEFAULT 'open',   -- open | sealed | revealed
            winner_count      INTEGER NOT NULL,
            seed_commitment   TEXT NOT NULL,
            admin_token       TEXT NOT NULL,
            rule_id           TEXT NOT NULL,
            rule_version      INTEGER NOT NULL,
            rule_description  TEXT NOT NULL,
            participants_json TEXT,
            participants_digest TEXT,
            participant_count INTEGER,
            winners_json      TEXT,
            seed              TEXT,
            created_at        TEXT NOT NULL,
            sealed_at         TEXT,
            revealed_at       TEXT
        );
        CREATE TABLE IF NOT EXISTS participants (
            round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
            id       INTEGER NOT NULL,
            label    TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (round_id, id)
        );
        """
    )


def body_json():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    return data


def require_token(round_row, data):
    token = str(data.get("admin_token", ""))
    if not token or not hmac.compare_digest(token, round_row["admin_token"]):
        raise ApiError(403, "管理令牌无效")


def get_round(conn, rid):
    row = conn.execute("SELECT * FROM rounds WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise ApiError(404, f"轮次 {rid} 不存在")
    return row


def parse_participants(raw):
    """把 [{id,label}, ...] 规范化为 [(id:int, label:str), ...] 并校验。"""
    if not isinstance(raw, list):
        raise ApiError(400, "participants 必须是数组")
    if len(raw) > MAX_PARTICIPANTS:
        raise ApiError(400, f"参与者人数超过上限 {MAX_PARTICIPANTS}")

    out = []
    seen = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ApiError(400, f"第 {i + 1} 项必须是对象")
        raw_id = item.get("id")
        if isinstance(raw_id, bool):
            raise ApiError(400, f"第 {i + 1} 项编号无效")
        try:
            pid = int(raw_id)
        except (TypeError, ValueError):
            raise ApiError(400, f"第 {i + 1} 项编号必须是整数")
        if not (ID_MIN <= pid <= ID_MAX):
            raise ApiError(400, f"第 {i + 1} 项编号超出允许范围 ({ID_MIN}..{ID_MAX})")
        if pid in seen:
            raise ApiError(400, f"编号重复: {pid}")
        seen.add(pid)

        label = item.get("label", "")
        if label is None:
            label = ""
        if not isinstance(label, str):
            raise ApiError(400, f"编号 {pid} 的备注必须是字符串")
        label = label.strip()
        if len(label) > MAX_LABEL_LEN:
            raise ApiError(400, f"编号 {pid} 的备注过长 (上限 {MAX_LABEL_LEN} 字)")
        if any(ch in label for ch in "\r\n\t"):
            raise ApiError(400, f"编号 {pid} 的备注不能包含制表符或换行")
        out.append((pid, label))
    return out


def canonical_lines(participants):
    """规范名单: 按编号数值升序, 每行 '编号<TAB>备注', 行之间以 LF 分隔, 无行尾换行。"""
    ordered = sorted(participants, key=lambda p: p[0])
    return "\n".join(f"{pid}\t{label}" for pid, label in ordered)


def participants_digest(canonical_text):
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def score(seed, pid):
    return hashlib.sha256(f"{seed}:{pid}".encode("utf-8")).hexdigest()


def pick_winners(seed, participants, winner_count):
    scored = [(score(seed, pid), pid, label) for pid, label in participants]
    scored.sort(key=lambda t: (t[0], t[1]))
    chosen = scored[:winner_count]
    return [
        {"rank": i + 1, "id": pid, "label": label, "hash": h}
        for i, (h, pid, label) in enumerate(chosen)
    ]


def compact_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def round_public(conn, row):
    """对外状态: 任何阶段都可查看; 封存后以快照为准。"""
    base = {
        "id": row["id"],
        "status": row["status"],
        "winner_count": row["winner_count"],
        "seed_commitment": row["seed_commitment"],
        "rule_id": row["rule_id"],
        "rule_version": row["rule_version"],
        "rule_description": row["rule_description"],
        "created_at": row["created_at"],
        "sealed_at": row["sealed_at"],
        "revealed_at": row["revealed_at"],
    }
    if row["status"] == "open":
        parts = [
            {"id": r["id"], "label": r["label"]}
            for r in conn.execute(
                "SELECT id, label FROM participants WHERE round_id=? ORDER BY id", (row["id"],)
            )
        ]
        base["participant_count"] = len(parts)
        base["participants"] = parts
    else:
        snap = json.loads(row["participants_json"])
        base["participants"] = snap
        base["participant_count"] = row["participant_count"]
        base["participants_digest"] = row["participants_digest"]
        base["canonical_list"] = canonical_lines([(p["id"], p["label"]) for p in snap])
    if row["status"] == "revealed":
        base["seed"] = row["seed"]
        base["winners"] = json.loads(row["winners_json"])
    return base


# --------------------------------------------------------------------------- 页面

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": now_iso()})


# --------------------------------------------------------------------------- API

@app.post("/api/rounds")
def create_round():
    data = body_json()
    commitment = str(data.get("seed_commitment", "")).strip().lower()
    if not HEX_RE.match(commitment):
        raise ApiError(400, "seed_commitment 必须是 32~128 位十六进制 SHA-256 承诺")

    raw_count = data.get("winner_count")
    if isinstance(raw_count, bool):
        raise ApiError(400, "winner_count 必须是正整数")
    try:
        winner_count = int(raw_count)
    except (TypeError, ValueError):
        raise ApiError(400, "winner_count 必须是正整数")
    if not (1 <= winner_count <= MAX_PARTICIPANTS):
        raise ApiError(400, f"winner_count 必须在 1..{MAX_PARTICIPANTS} 之间")

    token = secrets.token_urlsafe(24)
    db = get_db()
    cur = db.execute(
        """INSERT INTO rounds (winner_count, seed_commitment, admin_token, rule_id,
                               rule_version, rule_description, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (winner_count, commitment, token, RULE_ID, RULE_VERSION, RULE_DESCRIPTION, now_iso()),
    )
    rid = cur.lastrowid
    return jsonify(
        {
            "ok": True,
            "id": rid,
            "admin_token": token,
            "status": "open",
            "seed_commitment": commitment,
            "winner_count": winner_count,
            "rule_id": RULE_ID,
        }
    ), 201


@app.get("/api/rounds/<int:rid>")
def get_round_api(rid):
    return jsonify(round_public(get_db(), get_round(get_db(), rid)))


@app.post("/api/rounds/<int:rid>/participants/set")
def set_participants(rid):
    """封存前可反复整体替换名单; 封存后一律拒绝。"""
    db = get_db()
    row = get_round(db, rid)
    data = body_json()
    require_token(row, data)
    if row["status"] != "open":
        raise ApiError(409, f"轮次已{ '封存' if row['status']=='sealed' else '揭晓' }，名单不可修改")

    participants = parse_participants(data.get("participants"))
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT status FROM rounds WHERE id=?", (rid,)).fetchone()
        if row["status"] != "open":
            raise ApiError(409, "轮次已封存或揭晓，名单不可修改")
        db.execute("DELETE FROM participants WHERE round_id=?", (rid,))
        db.executemany(
            "INSERT INTO participants (round_id, id, label) VALUES (?, ?, ?)",
            [(rid, pid, label) for pid, label in participants],
        )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return jsonify({"ok": True, "id": rid, "participant_count": len(participants)})


@app.post("/api/rounds/<int:rid>/seal")
def seal_round(rid):
    db = get_db()
    row = get_round(db, rid)
    data = body_json()
    require_token(row, data)

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM rounds WHERE id=?", (rid,)).fetchone()
        if row["status"] != "open":
            raise ApiError(409, f"轮次当前状态为 {row['status']}，无法封存")

        participants = [
            (r["id"], r["label"])
            for r in db.execute("SELECT id, label FROM participants WHERE round_id=? ORDER BY id", (rid,))
        ]
        if len(participants) < row["winner_count"]:
            raise ApiError(
                400,
                f"人数不足: 当前 {len(participants)} 人，"
                f"至少需要 {row['winner_count']} 人才能封存",
            )

        canonical = canonical_lines(participants)
        digest = participants_digest(canonical)
        snapshot = [{"id": pid, "label": label} for pid, label in participants]
        sealed_at = now_iso()
        cur = db.execute(
            """UPDATE rounds
               SET status='sealed', participants_json=?, participants_digest=?,
                   participant_count=?, sealed_at=?
               WHERE id=? AND status='open'""",
            (compact_json(snapshot), digest, len(snapshot), sealed_at, rid),
        )
        if cur.rowcount != 1:  # 并发保护: 仅当仍为 open 时封存成功
            raise ApiError(409, "轮次状态已变化，封存失败")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    row = get_round(db, rid)
    return jsonify(
        {
            "ok": True,
            "id": rid,
            "status": "sealed",
            "sealed_at": sealed_at,
            "participant_count": row["participant_count"],
            "participants_digest": row["participants_digest"],
            "canonical_list": canonical,
        }
    )


@app.post("/api/rounds/<int:rid>/reveal")
def reveal_round(rid):
    """提交种子揭晓。承诺不匹配 -> 保持封存; 已揭晓 -> 原样返回, 绝不重抽。"""
    db = get_db()
    row = get_round(db, rid)
    data = body_json()
    require_token(row, data)

    seed = str(data.get("seed", "")).strip().lower()
    if not HEX_RE.match(seed):
        raise ApiError(400, "seed 必须是 32~128 位十六进制字符串")

    # 已经揭晓: 幂等返回, 永远不会用新种子重抽
    if row["status"] == "revealed":
        return (
            jsonify(
                {
                    "ok": True,
                    "id": rid,
                    "status": "revealed",
                    "already_revealed": True,
                    "winners": json.loads(row["winners_json"]),
                    "seed": row["seed"],
                }
            ),
            409,
        )
    if row["status"] != "sealed":
        raise ApiError(409, "轮次尚未封存，不能揭晓")

    if hashlib.sha256(seed.encode("utf-8")).hexdigest() != row["seed_commitment"]:
        # 关键安全性质: 承诺不符时不做任何状态变更, 封存状态保持
        raise ApiError(409, "种子与事先承诺不匹配，轮次保持封存状态")

    snapshot = [(p["id"], p["label"]) for p in json.loads(row["participants_json"])]
    winners = pick_winners(seed, snapshot, row["winner_count"])
    revealed_at = now_iso()

    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute(
            """UPDATE rounds
               SET status='revealed', seed=?, winners_json=?, revealed_at=?
               WHERE id=? AND status='sealed'""",
            (seed, compact_json(winners), revealed_at, rid),
        )
        if cur.rowcount != 1:  # 并发揭晓保护
            raise _ConflictOrRevealed(rid)
        db.execute("COMMIT")
    except _ConflictOrRevealed as exc:
        db.execute("ROLLBACK")
        row = db.execute("SELECT * FROM rounds WHERE id=?", (exc.rid,)).fetchone()
        if row["status"] == "revealed":
            return (
                jsonify(
                    {
                        "ok": True,
                        "id": rid,
                        "status": "revealed",
                        "already_revealed": True,
                        "winners": json.loads(row["winners_json"]),
                        "seed": row["seed"],
                    }
                ),
                409,
            )
        raise ApiError(409, "状态冲突，揭晓失败")
    except BaseException:
        db.execute("ROLLBACK")
        raise

    return jsonify(
        {
            "ok": True,
            "id": rid,
            "status": "revealed",
            "already_revealed": False,
            "revealed_at": revealed_at,
            "seed": seed,
            "winners": winners,
        }
    )


@app.get("/api/rounds/<int:rid>/package")
def export_package(rid):
    """导出确定性核验包 (紧凑 JSON, 键序固定), 供独立核验器离线重算。"""
    db = get_db()
    row = get_round(db, rid)
    if row["status"] != "revealed":
        raise ApiError(409, f"轮次当前状态为 {row['status']}，揭晓后才能导出核验包")

    snapshot = json.loads(row["participants_json"])
    winners = json.loads(row["winners_json"])
    canonical = canonical_lines([(p["id"], p["label"]) for p in snapshot])
    package = {
        "package_version": 1,
        "exported_at": now_iso(),
        "round": {
            "id": row["id"],
            "status": row["status"],
            "winner_count": row["winner_count"],
            "created_at": row["created_at"],
            "sealed_at": row["sealed_at"],
            "revealed_at": row["revealed_at"],
        },
        "commitment": {
            "algorithm": "SHA-256",
            "seed_commitment": row["seed_commitment"],
            "note": "创建轮次前由浏览器对种子计算 SHA-256(UTF8(seed)) 得到, 应事先公开留存",
        },
        "participants": {
            "count": row["participant_count"],
            "canonical_encoding": "按编号数值升序; 每行 '编号<TAB>备注'; 行之间 LF; 无行尾换行",
            "canonical_list": canonical,
            "digest_algorithm": "SHA-256",
            "digest": row["participants_digest"],
        },
        "rule": {
            "rule_id": row["rule_id"],
            "rule_version": row["rule_version"],
            "description": row["rule_description"],
            "score_encoding": "lowercase hex SHA-256 of UTF-8 bytes of seed + ':' + decimal id",
            "tie_break": "若两个 score 相同(实践中不会发生), 编号较小者优先",
        },
        "reveal": {
            "seed": row["seed"],
            "seed_encoding": "lowercase hex",
        },
        "results": {
            "winner_count": len(winners),
            "winners": winners,  # [{rank,id,label,hash}], hash 即该编号的 score
        },
    }

    payload = json.dumps(package, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    filename = f"draw-package-round-{rid}.json"
    resp = Response(payload, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
