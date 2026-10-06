# 可公开核验的现场抽签（Flask + SQLite + 原生 JavaScript）

一个可以放在现场大屏上、任何人都能事后独立验证结果的抽签网页。

- **先承诺、后揭晓**：创建轮次时种子只在你浏览器里生成，服务端只收到它的 SHA-256
  承诺和中奖名额；请立即保存种子，并把承诺提前张贴公示。
- **名单封存**：按唯一编号登记，封存前可反复修改；封存要求人数 ≥ 名额，并固化
  「按编号排序的规范名单快照 + SHA-256 摘要」，封存后服务端拒绝任何修改。
- **确定性开奖**：提交的种子与承诺匹配才开奖，按下面公开的 `pick-sha256-v1`
  规则从封存名单中确定性选出不重复中奖者；结果与状态一次写入，**重复揭晓不会重抽**。
- **可独立核验**：导出含种子、名单、摘要、规则、结果的核验包；任何人结合**事先留存
  的承诺和名单摘要**即可离线重算，而不是只比对服务端存好的中奖列表。
- 无外部账号、无付费服务、无 CDN，前端零依赖；单容器 + SQLite，一条命令启动。

## 一、一键启动（Docker Compose）

```bash
docker compose up -d --build
```

访问地址：

| 用途 | 地址 |
| --- | --- |
| 抽签网页（创建 / 管理 / 公示 / 核验都在这一页） | <http://localhost:8080/> |
| 健康检查 | <http://localhost:8080/healthz> |

- 宿主机端口 **8080**，容器内也是 8080；改端口编辑 `docker-compose.yml` 里的
  `"宿主端口:8080"`。
- SQLite 数据保存在 Docker 卷 `draw-data`（容器内 `/data/draw.db`），
  重建容器数据不丢。
- 停止：`docker compose down`（加 `-v` 会同时删除数据卷）。

不用 Docker 也可以直接跑（需要 Python 3.10+）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 app.py            # 监听 0.0.0.0:8080；PORT 环境变量可改端口
```

## 二、页面上的完整操作示例

1. 打开 <http://localhost:8080/>，在「① 创建轮次」填好中奖名额（如 3），点
   **生成随机种子**。页面会显示：
   - `seed`（64 位十六进制，32 字节随机）——点「下载种子凭证 .txt」**立即保存**，
     揭晓前不要泄露，服务端不保存种子，丢失即无法揭晓；
   - `commitment = SHA-256(seed)`——把它抄/截图/张贴出去，作为事先承诺。
2. 点「② 仅把承诺+名额提交」。服务端创建轮次后给你两个链接：
   - **管理链接**（URL 里带 `token`，仅主持人保存，凭它封存/揭晓）；
   - **公示链接**（只有 `id`，任何人可看进度与结果，不能修改）。
3. 用管理链接进入「② 轮次管理」，在文本框里登记参与者，每行
   `编号,备注`（编号为正整数且唯一，备注可空），点「保存名单」。封存前可反复修改。
4. 人数不少于名额后点「🔒 封存名单」。页面展示冻结后的规范名单和
   **名单摘要 SHA-256**——请同样公示/截图留存，封存后名单不可再改。
5. 到揭晓时刻，粘贴种子点「提交种子揭晓」：
   - 种子与承诺不匹配 → 页面报错，轮次**保持封存**，可以换正确种子重试；
   - 匹配 → 立即展示名次、中奖编号、备注及每个编号的散列值。
6. 点「导出核验包 .json」，得到 `draw-package-round-<id>.json`。
7. 公示：把**核验包**连同第 1、4 步留存的承诺、名单摘要一起发布。

## 三、独立核验（两种方式，结论互不依赖服务端）

### 方式 A：网页内核验（不联网也能做）

打开 <http://localhost:8080/#/verify>，选择核验包 JSON，并填入事先留存的承诺与
名单摘要（留空则只检查包内自洽）。页面会逐项重算并指出不符之处，包括：

- 重算 `SHA-256(seed)` 与承诺、与事先留存承诺是否一致；
- 重新解析规范名单、校验排序与编号唯一、重算摘要并与事先摘要比对；
- **按公开规则对全名单重新排序、重新选出全部中奖者**，再与包内结果逐字段比对
  （编号、名次、备注、每个 `SHA-256(seed:id)`）。

### 方式 B：命令行核验器（只用 Python 标准库）

```bash
python3 verify_package.py draw-package-round-1.json \
  --commitment <事先公示的承诺> \
  --digest <封存时公示的名单摘要>
```

全部一致退出码为 `0` 并打印 `核验通过: N/N 项`；任何一项不符退出码为 `1`
并逐项列出 FAIL。核验器与服务端代码完全独立，也可以拷到任意一台装了
Python 3 的机器上离线运行。

快速体验防篡改效果：

```bash
python3 - <<'EOF'
import json
p = json.load(open("draw-package-round-1.json"))
p["results"]["winners"][0]["id"] = 999999        # 改中奖名单
json.dump(p, open("tampered.json","w"))
EOF
python3 verify_package.py tampered.json          # 必然 FAIL，且会标出与独立重算不符
```

## 四、公开的确定性规则：`pick-sha256-v1`

1. **规范名单**：封存时把参与者按编号数值升序排列，每行 `编号<TAB>备注`
   （TAB 为水平制表符），行之间用 `\n`（LF）分隔，无行尾换行；其 UTF-8
   字节的 SHA-256 即**名单摘要**。
2. **承诺**：`commitment = lowercase(hex(SHA-256(UTF-8(seed))))`，
   种子为小写十六进制字符串（页面生成 64 位，即 32 随机字节）。
3. **评分**：对名单中每个编号 `id` 计算
   `score(id) = lowercase(hex(SHA-256(UTF-8(seed + ":" + decimal(id)))))`。
4. **名次**：所有编号按 `(score 升序, 编号升序)` 排序（编号升序仅为理论上的
   平局兜底），前 `winner_count` 个即中奖者，天然不重复；`rank` 从 1 开始。

规则标识、版本与文字说明同时存入数据库快照、核验包（`rule.rule_id` 等字段），
服务端 Python、浏览器 JavaScript、独立核验器三处实现相同算法，仓库测试
（`tests/e2e_test.py`）会交叉验证。

## 五、HTTP API（也可以用 curl 走完整流程）

```bash
# 1) 本地生成种子与承诺(例如)
SEED=$(python3 -c "import secrets;print(secrets.token_hex(32))")
COMMIT=$(python3 -c "import hashlib,os;print(hashlib.sha256(os.environ['SEED'].encode()).hexdigest())" )
echo "保存种子: $SEED"; echo "公示承诺: $COMMIT"

# 2) 创建轮次(只上传承诺与名额)
curl -s -X POST localhost:8080/api/rounds -H 'Content-Type: application/json' \
  -d "{\"seed_commitment\":\"$COMMIT\",\"winner_count\":3}"

# 3) 登记(把 $ID / $TOKEN 换成上一步返回值; 封存前可反复整体替换)
curl -s -X POST localhost:8080/api/rounds/$ID/participants/set \
  -H 'Content-Type: application/json' \
  -d "{\"admin_token\":\"$TOKEN\",\"participants\":[{\"id\":1,\"label\":\"甲\"},{\"id\":2,\"label\":\"乙\"},{\"id\":3,\"label\":\"丙\"}]}"

# 4) 封存
curl -s -X POST localhost:8080/api/rounds/$ID/seal \
  -H 'Content-Type: application/json' -d "{\"admin_token\":\"$TOKEN\"}"

# 5) 揭晓
curl -s -X POST localhost:8080/api/rounds/$ID/reveal \
  -H 'Content-Type: application/json' -d "{\"admin_token\":\"$TOKEN\",\"seed\":\"$SEED\"}"

# 6) 导出核验包
curl -sOJ localhost:8080/api/rounds/$ID/package
```

其余接口：`GET /api/rounds/<id>`（任意人查看状态/名单/结果）、
`GET /healthz`。所有写操作都要在 JSON body 里带正确 `admin_token`。

## 六、安全性质与边界

- 种子从创建到揭晓都不经过服务端；服务端只有承诺，无法提前算出结果。
- 封存使用 SQLite `BEGIN IMMEDIATE` 事务，快照、摘要、状态一次写入；
  封存/揭晓的状态迁移带条件更新，防止并发重复操作。
- 揭晓接口对已揭晓轮次是幂等的：返回既有结果并带 `already_revealed: true`，
  **不会**用后来提交的任何种子重新开奖。
- 管理令牌（`secrets.token_urlsafe`）是修改/揭晓的唯一凭据，请通过管理链接
  妥善保存；公示链接不含令牌。
- 容量上限：单次请求 16MB、最多 100,000 名参与者、编号范围 1..10^12、备注 ≤100 字。
- 要防的是「服务端/主持人事后篡改」，前提是承诺与名单摘要确实在开奖前已公开留存。

## 七、运行测试

```bash
pip install flask      # 仅测试需要; verify_package.py 本身零依赖
python3 tests/e2e_test.py
```

测试覆盖：参数校验、封存前改名单、人数不足拒封、封存后拒改、错种子保持封存、
正常开奖与三方结果一致、重复揭晓不重抽、核验包导出，以及对结果/名单/种子的
多类篡改必须被独立核验器抓出。

## 目录结构

```
app.py                  Flask + SQLite 后端
templates/index.html    单页界面(创建/管理/公示/核验)
static/app.js           原生 JS: 种子生成、承诺、管理、浏览器端独立重算
static/style.css
verify_package.py       独立命令行核验器(仅标准库)
tests/e2e_test.py       端到端测试
Dockerfile / docker-compose.yml / requirements.txt
```
