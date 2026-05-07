# style_config

本地的「图像生成风格库」管理后台。

每条「风格」就是一个 prompt 模板 + 1-2 张参考图 + 一个画图模型，
比如「现代 YA 图像小说画风 + 这张参考图 + gpt-image-2」。
存好之后做角色立绘、场景图就直接选风格、填外形，不用每次复制粘贴长 prompt。

栈：Flask + Postgres + 阿里云 OSS（存图）+ mob-ai（默认调模型）/ Zenmux（fallback）。
单机用，前端是一个 HTML + 原生 JS。

## 跑起来

```bash
pip install flask oss2 'psycopg[binary]' google-genai keyring
# 你得有本地的 Postgres，建好库（首次）：
createdb style_config

# 1) 把非敏感配置写到 .env（看下面模板）
# 2) 把 OSS / Zenmux key 写进 macOS 钥匙串：
python3 -c "import secrets_helper as s; \
  s.set_secret('OSS_ACCESS_KEY_ID', 'LTAI...'); \
  s.set_secret('OSS_ACCESS_KEY_SECRET', '...'); \
  s.set_secret('ZENMUX_API_KEY', 'sk-...')"
#    （或者把 key 临时写进 .env 跑一次 python3 migrate_secrets.py 自动迁，迁完
#     migrate_secrets.py 会把 .env 里的敏感行清掉）

python3 server.py
# → http://localhost:5050
```

**`.env` 只放非敏感配置**，敏感的（key / 密码）走系统钥匙串。详见
[「密钥管理」](#密钥管理)。

```
# .env
OSS_REGION=us-west-1
OSS_BUCKET=your-bucket
OSS_ENDPOINT=https://oss-us-west-1.aliyuncs.com
OSS_PREFIX=style-config

MOB_AI_BASE_URL=https://ai.mob-ai.cn
ZENMUX_VERTEX_BASE_URL=https://zenmux.ai/api/vertex-ai

DATABASE_URL=postgresql:///style_config
PORT=5050
```

## 长这样

打开 `http://localhost:5050` 是个表格，一条风格一行。每行有：

- `id` / `style_name` / `type` / `model` / `prompt`（单行，超出省略号）
- `ref_preview`：参考图 64×64 缩略图。鼠标悬上去会弹出 URL + 复制按钮，点图本身开大图
- `generated_preview`：默认空，旁边有「生成」按钮。点了之后输入一段外形描述（用来替换 prompt 里的 `{{appearance}}`），后端调模型出一张样图存 OSS，缩略图回到这一格
- 「编辑」按钮：右侧滑出抽屉，改名 / 改 prompt / 换参考图都在这里

## 类别 / 模型枚举

前端下拉分两组：

**mob-ai (默认)** — 走 `https://ai.mob-ai.cn/api/v1/generations`：

| 模型 | 备注 |
|---|---|
| `image-gemini-pro` | 高质量、复杂构图，对应原 `google/gemini-3-pro-image-preview` |
| `image-gemini-flash` | 快速预览，对应原 `google/gemini-3.1-flash-image-preview` |
| `image-gpt` | 风格化插画，对应原 `openai/gpt-image-2` |

**zenmux (备用)** — 走 google-genai SDK，仅当一条 style 显式选了旧名时启用：

| 模型 | 备注 |
|---|---|
| `openai/gpt-image-2` | 老路径 |
| `google/gemini-3.1-flash-image-preview` | 老路径 |
| `google/gemini-3-pro-image-preview` | 老路径 |

`server.py` 按 model 名前缀分发：`image-*` → mob-ai，其他 → zenmux。
要回滚某条 style 到 zenmux，把它的 `model` 字段改回旧名即可，无需重启服务。

## 预览图是怎么来的

1. 你在「生成」窗里写一段外形，比如 `18-year-old young woman, casual sweater + jeans`
2. 后端把 prompt 里的 `{{appearance}}` 替换成这段（没 `{{appearance}}` 就贴在末尾）
3. 按 model 名前缀分发：
   - `image-*` → mob-ai：`POST /api/v1/generations`，body 直接带参考图 URL（mob-ai 自己拉），返回的 JSON 里有图片 URL
   - 其他（`openai/...` / `google/...`）→ zenmux：用 `google-genai` SDK，先把参考图下载成 bytes 再传给 SDK
4. 拿到 PNG bytes 后上传我们自己的 OSS（`style-config/previews/<id>-<tag>.png`），URL 写回 DB

预览图会持久化，以后打开还在；点「重生成」再写一段 appearance 就覆盖。

## DB schema

一张表 `styles`：

```
id                            text primary key   -- 12 位 hex
name                          text
category                      text
model                         text
prompt                        text               -- 含 {{appearance}} 占位符
reference_urls                jsonb              -- ["https://...", "https://..."]
generated_preview_url         text               -- 可空
generated_preview_appearance  text               -- 可空，记上次生成用的外形
created_at                    double precision   -- unix 秒
updated_at                    double precision
```

迁移就是 `server.py` 启动时 `CREATE TABLE IF NOT EXISTS`，没用 alembic / 之类的。
schema 简单，手写 SQL 改就行。

## HTTP API

```bash
# 列表
curl localhost:5050/api/styles

# 新建
curl -X POST localhost:5050/api/styles \
  -F name="YA 图像小说" \
  -F category="character series illustration" \
  -F model="image-gpt" \
  -F prompt='现代 YA 图像小说风格, ... {{appearance}}' \
  -F references=@/path/to/ref.png

# 编辑（references 不传就保留旧的）
curl -X PUT localhost:5050/api/styles/<id> \
  -F name="新名字" -F category=... -F model=... -F prompt=...

# 生成预览
curl -X POST localhost:5050/api/styles/<id>/preview \
  -H "Content-Type: application/json" \
  -d '{"appearance":"18yo woman, casual sweater"}'

# 删除（OSS 上的图不会删）
curl -X DELETE localhost:5050/api/styles/<id>
```

字段验证有问题的时候返回 `400 {"field_errors":{...}}`，前端按字段显示错误。

## 远程部署 + MCP（可选）

如果要让多台机器 / Claude 共享同一份风格库，仓库里有个独立 PG + MCP 的方案：

- 远端 PG（默认绑 `127.0.0.1`，不暴露公网）
- 本地 MCP server 用 SSH 隧道连远端，给 Claude 暴露 6 个 tool（list / get / create / update / delete / set_generated_preview）

```bash
pip install mcp 'psycopg[binary]' sshtunnel 'paramiko<4.0'
# 编辑 mcp/.env 填 SSH + PG 密码
# 注册到 Claude Code：
claude mcp add style_config --scope user -- python3 $(pwd)/mcp/style_config_mcp.py
```

注意：

- MCP 只管 DB，不管 OSS。`create_style` / `update_style` 收的是已经在 OSS 上的 URL；
  上传图还是用 web 后台的接口
- `paramiko<4.0` 的限制是因为 `sshtunnel 0.4.0` 还引用了 4.0 移除的 `DSSKey`
- 远端 PG 建库 / 建 role 的 SQL 在 git 历史里能找到，没单独写脚本

## 密钥管理

`.env` 只存非敏感的配置（OSS bucket 名、endpoint、prefix、端口、PG 连接串
里的库名等）。所有 key 和密码 —— OSS access key、Zenmux API key、SSH 密码、
PG 密码 —— 都进 macOS Keychain，不落盘。

**为什么这样：** `.env` 文件很容易在备份、误传 git、屏幕共享时泄露。
Keychain 是系统级加密存储，跟你的登录密码绑定，即使 `.env` 被人看到也拿不到 key。

**敏感名清单**（在 `secrets_helper.py` 里）：

```
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
ZENMUX_API_KEY      # 老 backend，fallback 路径还要用
MOB_AI_API_KEY      # 当前 image backend
SSH_PASSWORD
PG_PASSWORD
STYLE_CONFIG_TOKEN
```

代码里 `must(key)` / `env(key)` 看到这些名字就只查 `os.environ` 和 Keychain，
绝不查 `.env`。

### 怎么放进去

- 把现有 `.env` 里的敏感值迁过去：`python3 migrate_secrets.py`（迁完会把
  `.env` 里那几行删掉）
- 或者手动加：
  ```python
  import secrets_helper as s
  s.set_secret("OSS_ACCESS_KEY_ID", "LTAI...")
  ```
- 或者 GUI：打开「钥匙串访问」搜 `style_config`

### 怎么换 key

发现 key 泄露了：
1. 去对应平台后台撤销并新建（OSS console / Zenmux 后台 / SSH 改密码 / PG 改密码）
2. `s.set_secret("OSS_ACCESS_KEY_ID", "新值")` 覆盖

### 这套不能防什么

- 你的 Mac 被入侵 / 木马跑在你账号下：木马照样能调 Keychain，跟 `.env` 一样裸
- 你把 key 贴进 chat / issue / 截图：覆水难收，立刻去对应平台轮换
- 不在 macOS 上跑：`keyring` 在 Linux 用 libsecret / Secret Service，Windows 用
  Credential Manager，得装系统对应后端

## 限制 / 已知问题

- 没鉴权，本地用，别开公网
- 删除只清 DB row，OSS 上的图不删（方便排查 + 省事）
- 预览生成是同步的，POST 期间会卡 10–60 秒
- 远端 PG 和本地 PG 不会自动同步，是两套独立库
- 移动端 hover 没法触发 tooltip，但点缩略图开大图能用

## 目录

```
server.py                   Flask 后端
public/index.html           前端（单文件，原生 JS + 自写 Material 3 风格 CSS）
secrets_helper.py           查 Keychain 的胶水
migrate_secrets.py          一次性迁移：.env → Keychain
mcp/style_config_mcp.py     MCP server（可选）
mcp/.env                    MCP 非敏感配置（SSH host/user 等，gitignore）
.env                        Web 非敏感配置（OSS bucket / endpoint 等，gitignore）
```
