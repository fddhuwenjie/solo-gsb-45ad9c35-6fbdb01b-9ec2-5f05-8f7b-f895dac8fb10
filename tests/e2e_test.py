"""端到端测试: 覆盖承诺/封存/揭晓幂等/防篡改核验等关键安全性质。

运行: python3 tests/e2e_test.py   (需要先 pip install flask)
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp()
os.environ["DRAW_DB_PATH"] = os.path.join(_TMP, "test.db")

import app as flask_app  # noqa: E402
from verify_package import verify as verify_pkg  # noqa: E402


def seed_and_commitment(seed_hex):
    return seed_hex, hashlib.sha256(seed_hex.encode()).hexdigest()


def independent_pick(seed, participants, n):
    """核验器重算逻辑的本地副本(再次独立实现, 用于对照 API 结果)。"""
    scored = [(hashlib.sha256(f"{seed}:{pid}".encode()).hexdigest(), pid, label)
              for pid, label in participants]
    scored.sort(key=lambda t: (t[0], t[1]))
    return [{"rank": i + 1, "id": pid, "label": label, "hash": h}
            for i, (h, pid, label) in enumerate(scored[:n])]


SEED_A = "a1" * 32
SEED_B = "b2" * 32  # 另一个不同的种子, 用于"重复揭晓不得重抽"测试
COMMIT_A = hashlib.sha256(SEED_A.encode()).hexdigest()

PEOPLE = [(1001 + i, f"选手{i}") for i in range(20)]


class DrawFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        flask_app.app.testing = True
        cls.client = flask_app.app.test_client()

    def post(self, url, payload):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json")

    def create(self, commitment=COMMIT_A, winner_count=3):
        r = self.post("/api/rounds", {"seed_commitment": commitment, "winner_count": winner_count})
        self.assertEqual(r.status_code, 201, r.data)
        d = r.get_json()
        return d["id"], d["admin_token"]

    def set_participants(self, rid, token, people):
        r = self.post(f"/api/rounds/{rid}/participants/set",
                      {"admin_token": token, "participants": [{"id": i, "label": l} for i, l in people]})
        return r

    def test_00_health(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_01_create_validation(self):
        r = self.post("/api/rounds", {"seed_commitment": "zzz", "winner_count": 1})
        self.assertEqual(r.status_code, 400)
        r = self.post("/api/rounds", {"seed_commitment": COMMIT_A, "winner_count": 0})
        self.assertEqual(r.status_code, 400)

    def test_02_full_flow_and_verification(self):
        rid, token = self.create(winner_count=3)

        # 无令牌 / 错令牌
        r = self.set_participants(rid, "wrong", PEOPLE)
        self.assertEqual(r.status_code, 403)

        # 封存前可反复改名单
        self.assertEqual(self.set_participants(rid, token, PEOPLE[:5]).status_code, 200)
        self.assertEqual(self.set_participants(rid, token, PEOPLE).status_code, 200)

        # 重复编号被拒
        dup = [{"id": 1, "label": "x"}, {"id": 1, "label": "y"}]
        self.assertEqual(self.post(f"/api/rounds/{rid}/participants/set",
                                   {"admin_token": token, "participants": dup}).status_code, 400)

        # 人数不足不能封存
        rid2, token2 = self.create(commitment=COMMIT_A, winner_count=10)
        self.assertEqual(self.set_participants(rid2, token2, PEOPLE[:5]).status_code, 200)
        r = self.post(f"/api/rounds/{rid2}/seal", {"admin_token": token2})
        self.assertEqual(r.status_code, 400)

        # 正常封存
        r = self.post(f"/api/rounds/{rid}/seal", {"admin_token": token})
        self.assertEqual(r.status_code, 200, r.data)
        sealed = r.get_json()
        self.assertEqual(sealed["participant_count"], 20)
        digest = sealed["participants_digest"]
        canonical = sealed["canonical_list"]
        expect_digest = hashlib.sha256(
            "\n".join(f"{i}\t{l}" for i, l in PEOPLE).encode()).hexdigest()
        self.assertEqual(digest, expect_digest)

        # 封存后拒绝改名单
        r = self.set_participants(rid, token, PEOPLE[:10])
        self.assertEqual(r.status_code, 409)
        state = self.client.get(f"/api/rounds/{rid}").get_json()
        self.assertEqual(state["status"], "sealed")
        self.assertEqual(state["participant_count"], 20)  # 没有被覆盖

        # 错种子揭晓: 保持封存
        r = self.post(f"/api/rounds/{rid}/reveal", {"admin_token": token, "seed": SEED_B})
        self.assertEqual(r.status_code, 409)
        self.assertIn("承诺", r.get_json()["error"])
        state = self.client.get(f"/api/rounds/{rid}").get_json()
        self.assertEqual(state["status"], "sealed")
        self.assertNotIn("winners", state)

        # 正确种子揭晓
        r = self.post(f"/api/rounds/{rid}/reveal", {"admin_token": token, "seed": SEED_A})
        self.assertEqual(r.status_code, 200, r.data)
        winners = r.get_json()["winners"]
        expected = independent_pick(SEED_A, PEOPLE, 3)
        self.assertEqual(winners, expected)
        self.assertEqual(len({w["id"] for w in winners}), 3)

        # 重复揭晓(用另一个种子): 409 + 返回原结果, 绝不重抽
        r = self.post(f"/api/rounds/{rid}/reveal", {"admin_token": token, "seed": SEED_B})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["already_revealed"])
        self.assertEqual(r.get_json()["winners"], expected)
        state = self.client.get(f"/api/rounds/{rid}").get_json()
        self.assertEqual(state["status"], "revealed")
        self.assertEqual(state["seed"], SEED_A)

        # 揭晓后仍然不能改名单
        self.assertEqual(self.set_participants(rid, token, PEOPLE[:2]).status_code, 409)

        # 导出核验包
        r = self.client.get(f"/api/rounds/{rid}/package")
        self.assertEqual(r.status_code, 200)
        pkg = r.get_json()

        # 独立核验: 不带外部值(包内一致)
        rep = verify_pkg(pkg)
        self.assertTrue(rep.passed, [c for c in rep.checks if not c[0]])

        # 带上事先留存的承诺与名单摘要
        rep = verify_pkg(pkg, ext_commitment=COMMIT_A, ext_digest=expect_digest)
        self.assertTrue(rep.passed, [c for c in rep.checks if not c[0]])

        # 错误的外部承诺 -> 必须报错
        rep = verify_pkg(pkg, ext_commitment="00" * 32)
        self.assertFalse(rep.passed)

        # 篡改结果 -> 不能只比对已存列表, 重算会发现
        tampered = json.loads(json.dumps(pkg))
        tampered["results"]["winners"][0] = dict(tampered["results"]["winners"][1])
        tampered["results"]["winners"][0]["rank"] = 1
        rep = verify_pkg(tampered)
        self.assertFalse(rep.passed)
        self.assertTrue(any("独立重算" in c[1] for c in rep.checks if not c[0]))

        # 篡改名单但同步改摘要 -> 与重算结果不符; 外部摘要也能直接抓出
        tampered2 = json.loads(json.dumps(pkg))
        victim_id = winners[0]["id"]  # 篡改一名真实中奖者, 保证影响重算
        lines = tampered2["participants"]["canonical_list"].split("\n")
        lines = [("9999\t冒名顶替者" if line.startswith(f"{victim_id}\t") else line) for line in lines]
        new_canon = "\n".join(sorted(lines, key=lambda s: int(s.split("\t")[0])))
        tampered2["participants"]["canonical_list"] = new_canon
        tampered2["participants"]["digest"] = hashlib.sha256(new_canon.encode()).hexdigest()
        rep = verify_pkg(tampered2)  # 重算会发现结果与名单对不上
        self.assertFalse(rep.passed)
        rep2 = verify_pkg(tampered2, ext_digest=expect_digest)  # 外部摘要直接抓出
        self.assertFalse(rep2.passed)

        # 篡改种子(并同步承诺) -> 外部承诺抓出, 且重算结果不符
        tampered3 = json.loads(json.dumps(pkg))
        tampered3["reveal"]["seed"] = SEED_B
        tampered3["commitment"]["seed_commitment"] = hashlib.sha256(SEED_B.encode()).hexdigest()
        rep = verify_pkg(tampered3, ext_commitment=COMMIT_A)
        self.assertFalse(rep.passed)

    def test_03_package_only_after_reveal(self):
        rid, token = self.create()
        self.assertEqual(self.client.get(f"/api/rounds/{rid}/package").status_code, 409)
        people = [(i, f"p{i}") for i in range(1, 6)]
        self.set_participants(rid, token, people)
        self.post(f"/api/rounds/{rid}/seal", {"admin_token": token})
        self.assertEqual(self.client.get(f"/api/rounds/{rid}/package").status_code, 409)

    def test_04_missing_round(self):
        self.assertEqual(self.client.get("/api/rounds/999999").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
