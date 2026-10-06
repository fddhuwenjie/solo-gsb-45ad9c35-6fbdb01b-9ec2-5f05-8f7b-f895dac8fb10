#!/usr/bin/env python3
"""核验包独立核验器 —— 只依赖 Python 标准库。

用法:
    python3 verify_package.py draw-package-round-1.json \
        [--commitment <事先公开的SHA-256承诺>] \
        [--digest <封存时公示的名单摘要>]

它不会信任核验包里的 winners 列表, 而是:
  1. 重新计算 SHA-256(seed), 与包中承诺及(可选)事先留存的承诺比对;
  2. 自行解析 canonical_list 并重新计算名单摘要, 与包中摘要及(可选)事先摘要比对;
  3. 按 pick-sha256-v1 公开规则对全名单重新排序、重新选出全部中奖者;
  4. 逐字段比对编号/名次/备注/hash, 报告任何不符之处。

退出码: 0 = 全部通过; 1 = 存在不符或无法核验。
"""

import argparse
import hashlib
import json
import sys

RULE_ID = "pick-sha256-v1"
HEX_CHARS = set("0123456789abcdef")


class Report:
    def __init__(self):
        self.checks = []

    def check(self, ok, name, detail=""):
        self.checks.append((bool(ok), name, detail))

    @property
    def passed(self):
        return all(ok for ok, _, _ in self.checks)


def is_hex(s, length=None):
    return isinstance(s, str) and all(c in HEX_CHARS for c in s) and (length is None or len(s) == length)


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify(pkg, ext_commitment="", ext_digest=""):
    r = Report()

    if not isinstance(pkg, dict):
        r.check(False, "核验包顶层必须是 JSON 对象")
        return r

    required = ["package_version", "round", "commitment", "participants", "rule", "reveal", "results"]
    missing = [k for k in required if k not in pkg]
    if missing:
        for k in missing:
            r.check(False, f"缺少字段 package.{k}")
        return r

    round_, commit, parts, rule, reveal, results = (
        pkg["round"], pkg["commitment"], pkg["participants"],
        pkg["rule"], pkg["reveal"], pkg["results"],
    )

    seed = str(reveal.get("seed", "")).lower()
    stored_commitment = str(commit.get("seed_commitment", "")).lower()
    stored_digest = str(parts.get("digest", "")).lower()
    canonical = parts.get("canonical_list", "")
    n = round_.get("winner_count")

    # ---- 基本字段
    r.check(round_.get("status") == "revealed", "轮次状态为 revealed", f"实际: {round_.get('status')}")
    r.check(is_hex(seed) and len(seed) >= 32, "种子为合法的小写十六进制字符串(>=32位)", f"长度: {len(seed)}")
    r.check(rule.get("rule_id") == RULE_ID, f"规则标识为公开规则 {RULE_ID}", f"实际: {rule.get('rule_id')}")
    r.check(isinstance(n, int) and not isinstance(n, bool) and n >= 1,
            "中奖名额为正整数", f"实际: {n!r}")

    # ---- 1) 承诺: 独立重算
    calc_commitment = sha256_hex(seed) if is_hex(seed) else None
    if calc_commitment:
        r.check(calc_commitment == stored_commitment,
                "重算承诺 SHA-256(seed) 与包中承诺一致",
                f"重算={calc_commitment}\n包内={stored_commitment}")
        if is_hex(stored_commitment) and len(stored_commitment) != 64:
            r.check(False, "承诺长度应为 64 位 SHA-256 十六进制", f"实际长度: {len(stored_commitment)}")
        if ext_commitment:
            ext_commitment = ext_commitment.lower()
            r.check(is_hex(ext_commitment, 64), "外部承诺为 64 位十六进制")
            r.check(calc_commitment == ext_commitment,
                    "重算承诺与【事先留存的承诺】一致",
                    f"重算={calc_commitment}\n留存={ext_commitment}")

    # ---- 2) 名单: 自行解析规范文本, 校验排序/唯一, 重算摘要
    participants = []
    list_ok = True
    if isinstance(canonical, str) and canonical != "":
        ids = []
        label_by_id = {}
        for i, line in enumerate(canonical.split("\n")):
            cols = line.split("\t")
            if len(cols) != 2 or not cols[0].isdigit():
                r.check(False, f"规范名单第 {i + 1} 行不符合 '编号<TAB>备注' 格式", repr(line))
                list_ok = False
                continue
            pid = int(cols[0])
            if pid in label_by_id:
                r.check(False, f"名单中编号重复: {pid}")
                list_ok = False
            label_by_id[pid] = cols[1]
            ids.append(pid)

        r.check(ids == sorted(ids), "规范名单确按编号数值升序排列")
        if len(ids) != len(set(ids)):
            list_ok = False
        participants = [(pid, label_by_id[pid]) for pid in ids]

        r.check(parts.get("count") == len(participants),
                "名单人数字段与实际行数一致",
                f"字段={parts.get('count')} 实际={len(participants)}")
        if parts.get("count") != len(participants):
            list_ok = False

        calc_digest = sha256_hex(canonical)
        r.check(calc_digest == stored_digest,
                "重算名单摘要 SHA-256(canonical_list) 与包中摘要一致",
                f"重算={calc_digest}\n包内={stored_digest}")
        if ext_digest:
            ext_digest = ext_digest.lower()
            r.check(is_hex(ext_digest, 64), "外部名单摘要为 64 位十六进制")
            r.check(calc_digest == ext_digest,
                    "重算名单摘要与【事先留存的名单摘要】一致",
                    f"重算={calc_digest}\n留存={ext_digest}")
    else:
        r.check(False, "核验包缺少 canonical_list，无法独立重建名单")
        list_ok = False

    enough = isinstance(n, int) and len(participants) >= n
    r.check(enough, f"人数 {len(participants)} ≥ 中奖名额 {n}（封存前置条件）")

    # ---- 3) 结果: 按公开规则完全独立重算
    stored_winners = results.get("winners") if isinstance(results, dict) else None
    can_recompute = calc_commitment and list_ok and enough and isinstance(stored_winners, list)

    if can_recompute:
        scored = [(sha256_hex(f"{seed}:{pid}"), pid, label) for pid, label in participants]
        scored.sort(key=lambda t: (t[0], t[1]))
        expected = [
            {"rank": i + 1, "id": pid, "label": label, "hash": h}
            for i, (h, pid, label) in enumerate(scored[:n])
        ]

        r.check(results.get("winner_count") == n,
                "结果中的 winner_count 与轮次名额一致",
                f"结果={results.get('winner_count')} 名额={n}")
        r.check(len(stored_winners) == n,
                "中奖结果数量等于名额", f"结果数={len(stored_winners)} 名额={n}")
        got_ids = [w.get("id") for w in stored_winners]
        r.check(len(got_ids) == len(set(got_ids)), "中奖编号不重复", f"编号={got_ids}")

        labels = dict(participants)
        valid_ids = set(labels)
        for w in stored_winners:
            wid = w.get("id")
            if wid not in valid_ids:
                r.check(False, f"中奖编号 {wid} 不在名单内")
                continue
            if w.get("label") != labels[wid]:
                r.check(False, f"中奖编号 {wid} 的备注与名单快照不一致",
                        f"结果备注={w.get('label')!r} 名单备注={labels[wid]!r}")
            calc_hash = sha256_hex(f"{seed}:{wid}")
            if calc_hash != str(w.get("hash", "")).lower():
                r.check(False, f"编号 {wid} 的 hash 重算不符",
                        f"重算={calc_hash}\n包内={w.get('hash')}")

        exp_ids = [w["id"] for w in expected]
        r.check(got_ids == exp_ids,
                "【关键】按公开规则对全名单独立重算，中奖编号及名次与包内结果完全一致",
                f"独立重算={exp_ids}\n包内结果={got_ids}")
        ranks = [w.get("rank") for w in stored_winners]
        r.check(ranks == list(range(1, len(ranks) + 1)), "名次 rank 连续且从 1 开始", f"实际={ranks}")
    else:
        r.check(False, "因前置检查失败，无法独立重算结果（结果不可信）")

    return r


def main():
    ap = argparse.ArgumentParser(description="现场抽签核验包独立核验器 (pick-sha256-v1)")
    ap.add_argument("package", help="导出的核验包 JSON 文件")
    ap.add_argument("--commitment", default="", help="事先公开留存的种子承诺 SHA-256（可选但强烈建议）")
    ap.add_argument("--digest", default="", help="封存时公示的名单摘要 SHA-256（可选但强烈建议）")
    args = ap.parse_args()

    try:
        with open(args.package, "r", encoding="utf-8") as f:
            pkg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"无法读取/解析核验包: {e}")
        return 1

    report = verify(pkg, args.commitment, args.digest)
    passed = sum(1 for ok, _, _ in report.checks if ok)
    total = len(report.checks)

    print("=" * 72)
    for ok, name, detail in report.checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok and detail:
            for line in detail.splitlines():
                print(f"         {line}")
    print("=" * 72)
    if report.passed:
        print(f"核验通过: {passed}/{total} 项全部一致。")
        return 0
    print(f"核验未通过: {passed}/{total} 项 —— 存在上述不符之处，请勿采信该结果。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
