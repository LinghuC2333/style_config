# style_config

本地的「图像生成风格库」管理后台。

每条「风格」就是一个 prompt 模板 + 1-2 张参考图 + 一个画图模型，
比如「现代 YA 图像小说画风 + 这张参考图 + gpt-image-2」。
存好之后做角色立绘、场景图就直接选风格、填外形，不用每次复制粘贴长 prompt。

栈：Flask + Postgres + 阿里云 OSS（存图）+ Mob AI 路由（调模型生成预览图）。
单机用，前端是一个 HTML + 原生 JS。

## 跑起来

```bash
pip install flask oss2 'psycopg[binary]' keyring
# 你得有本地的 Postgres，建好库（首次）：
createdb style_config

# 1) 把非敏感配置写到 .env（看下面模板）
# 2) 把 OSS / Mob AI key 写进 macOS 钥匙串：
python3 -c "import secrets_helper as s; \
  s.set_secret('OSS_ACCESS_KEY_ID', 'LTAI...'); \
  s.set_secret('OSS_ACCESS_KEY_SECRET', '...'); \
  s.set_secret('MOB_AI_KEY', 'sk-...')"
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

MOB_AI_BASE_URL=https://ai.mob-ai.cn/api

DATABASE_URL=postgresql:///style_config
PORT=5050
```

## 长这样

打开 `http://localhost:5050` 是个表格，一条风格一行。每行有：

- `id` / `style_name` / `type` / `model` / `prompt`（单行，超出省略号）
- `ref_preview`：参考图 64×64 缩略图。鼠标悬上去会弹出 URL + 复制按钮，点图本身开大图
- `generated_preview`：默认空，旁边有「生成」按钮。点开后填替换文本（替换 prompt 里任意 `{{变量}}`）、可选上传任意多张参考图（按上传顺序）、可选挑输出比例（如 9:16），后端调模型出图、URL 写回 DB，缩略图回到这一格。悬停能看到出图 URL + 这次上传的参考图 URL
- 「编辑」按钮：右侧滑出抽屉，改名 / 改 prompt / 换参考图都在这里

## 类别 / 模型枚举

| 类别                              | 适用模型 |
|-----------------------------------|---|
| character series illustration     | openai/gpt-image-2 |
| character ep illustration         | google/gemini-3.1-flash-image-preview |
| scene series illustration         | google/gemini-3-pro-image-preview |
| scene ep illustration             | （留空，用哪个都行） |

不是强制约束，前端只是下拉选项；想加新模型直接改 `server.py` 里的 `VALID_MODELS`。生成时这些模型名会映射成 Mob AI 路由的 `image-gpt` / `image-gemini-pro` / `image-gemini-flash`（见 `server.py` 的 `MODEL_MAP`）。

## 预览图是怎么来的

1. 你在「生成」窗里写替换文本（替换 prompt 里任意 `{{变量}}`；没有占位符就贴末尾），可选上传任意多张参考图（保序）、可选挑输出比例
2. 喂给模型的参考图 = 风格已存的 refs（前）+ 这次上传的（后）
3. 后端调 Mob AI 路由 `POST /v1/generations`：model 映射成 `image-gpt` 等，参考图按 URL 传，比例走 `input.aspectRatio`
4. 路由返回图片 URL（已托管在 OSS），直接写回 DB；这次上传的参考图 URL 存进 `generated_preview_ref_urls`，比例存进 `generated_preview_aspect`

预览图会持久化，以后打开还在；点「重生成」覆盖。注意：出图同步等待、约 1–2 分钟；`aspectRatio` 只有 image-gpt 生效，gemini 会忽略。

## DB schema

一张表 `styles`：

```
id                            text primary key   -- 12 位 hex
name                          text
category                      text
model                         text
prompt                        text               -- 含 {{变量}} 占位符（任意名）
reference_urls                jsonb              -- ["https://...", "https://..."]
generated_preview_url         text               -- 可空
generated_preview_appearance  text               -- 可空，记上次生成用的替换文本
generated_preview_ref_urls    jsonb              -- 这次生成上传的参考图 URL（默认 []）
generated_preview_aspect      text               -- 可空，记上次用的输出比例（如 9:16）
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
  -F model="openai/gpt-image-2" \
  -F prompt='现代 YA 图像小说风格, ... {{appearance}}' \
  -F references=@/path/to/ref.png

# 编辑（references 不传就保留旧的）
curl -X PUT localhost:5050/api/styles/<id> \
  -F name="新名字" -F category=... -F model=... -F prompt=...

# 生成预览（multipart：appearance 必填；aspectRatio / references 可选，references 不限张数）
curl -X POST localhost:5050/api/styles/<id>/preview \
  -F appearance="18yo woman, casual sweater" \
  -F aspectRatio="9:16" \
  -F references=@/path/to/extra-ref.png

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

## 本地 ↔ 线上同步

不想直连线上库（也连不上），同步走线上的 HTTP API：

- `python3 sync_from_upstream.py` —— 把线上全部 style 拉进本地库（覆盖），本地 server 就成了线上的预发镜像
- `python3 sync_to_upstream.py` —— 把本地独有的 style（线上没有的，按名字判断）推到线上；线上会分配新 id，生成好的预览图不会带过去

两个脚本都只调 `https://style-config.mob-ai.cn/api/styles`（带 `STYLE_CONFIG_TOKEN`），不碰数据库。

## 密钥管理

`.env` 只存非敏感的配置（OSS bucket 名、endpoint、prefix、端口、PG 连接串
里的库名等）。所有 key 和密码 —— OSS access key、Mob AI key、SSH 密码、
PG 密码 —— 都进 macOS Keychain，不落盘。

**为什么这样：** `.env` 文件很容易在备份、误传 git、屏幕共享时泄露。
Keychain 是系统级加密存储，跟你的登录密码绑定，即使 `.env` 被人看到也拿不到 key。

**敏感名清单**（在 `secrets_helper.py` 里）：

```
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
MOB_AI_KEY
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
1. 去对应平台后台撤销并新建（OSS console / Mob AI 后台 / SSH 改密码 / PG 改密码）
2. `s.set_secret("OSS_ACCESS_KEY_ID", "新值")` 覆盖

### 这套不能防什么

- 你的 Mac 被入侵 / 木马跑在你账号下：木马照样能调 Keychain，跟 `.env` 一样裸
- 你把 key 贴进 chat / issue / 截图：覆水难收，立刻去对应平台轮换
- 不在 macOS 上跑：`keyring` 在 Linux 用 libsecret / Secret Service，Windows 用
  Credential Manager，得装系统对应后端

## 限制 / 已知问题

- 鉴权用 bearer / magic-link（`STYLE_CONFIG_TOKEN`，存 keychain）；不设 token 则鉴权关闭（纯本地时可以这样）
- 删除只清 DB row，OSS 上的图不删（方便排查 + 省事）
- 预览生成是同步的，POST 期间会等约 1–2 分钟（Flask 开了 threaded，其他请求不被卡）
- 远端 PG 和本地 PG 不会自动同步，是两套独立库
- 移动端 hover 没法触发 tooltip，但点缩略图开大图能用

## 目录

```
server.py                   Flask 后端
public/index.html           前端（单文件，原生 JS + 自写 Material 3 风格 CSS）
secrets_helper.py           查 Keychain 的胶水
migrate_secrets.py          一次性迁移：.env → Keychain
sync_from_upstream.py       从线上拉全部 style 到本地库（预发镜像）
sync_to_upstream.py         把本地独有的 style 推到线上（按名字去重）
mcp/style_config_mcp.py     MCP server（可选）
mcp/.env                    MCP 非敏感配置（SSH host/user 等，gitignore）
.env                        Web 非敏感配置（OSS bucket / endpoint 等，gitignore）
```
