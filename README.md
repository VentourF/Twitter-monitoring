# Twitter monitoring

监控 X (Twitter) 上指定用户（默认 [@thsottiaux](https://x.com/thsottiaux)）的新推文，一旦发帖就把推文内容通过 Telegram Bot 推送给你；另外内置**免费情报**功能：定时用关键词全网搜索 "free model / free credits / 限时免费" 这类推文，发现即推，每天再汇总一条日报，不用再自己刷 Twitter 找羊毛。

原理：用 [twikit](https://github.com/d60/twikit)（免费、不需要官方 API）定时轮询目标用户的推文列表，和本地记录的最新推文 ID 对比，发现新帖就调用 Telegram Bot API 推送。推送内容包括**清洗后的推文原文 + Bing 中文翻译 + 时间表述换算**（推文里带时区的时刻如 `tomorrow 10am PST` 会自动换算成北京时间并标注星期，夏令时自动处理；时长类表述如 `12h cache` 不会误报）。轮询间隔 90 秒，遇到限流自动退避重试，推送失败会保留进度下轮重试，重启后从 `state.json` 续跑。

## 一、准备工作

### 1. 创建 Telegram Bot（5 分钟）

1. 在 Telegram 里找 **@BotFather** → 发送 `/newbot` → 按提示起名 → 拿到一串 `bot_token`（形如 `123456789:AAH8x...`）。
2. **先给你的 bot 发一条任意消息**（比如 "hi"，这一步必须做，否则 bot 无法主动给你发消息）。
3. 浏览器打开下面这个地址获取你的 `chat_id`：
   `https://api.telegram.org/bot<把bot_token粘到这里>/getUpdates`
   在返回的 JSON 里找 `"chat":{"id":123456789,...}`，那串数字就是 `chat_id`。
   （也可以用 **@userinfobot**，它回复你的 Id 就是 chat_id。）

### 2. X 账号认证（推荐 cookie 方式，支持多账号轮换）

X 官方 API 免费档读不了推文，所以本项目走非官方接口，需要用已登录的 X 账号 cookie。**强烈建议用小号**，理论上有被风控的风险。

浏览器登录 x.com 后按 `F12` → **Application**（应用）标签 → 左侧 **Cookies** → `https://x.com`，找到并复制两个值：`auth_token`（登录凭证）和 `ct0`（CSRF token）。

面板「配置」页的账号框**每行一个账号**，格式 `auth_token:ct0:备注`（备注可省略），例如：

```
6e36cfb1...:a920cce5...:小号1
7f47dgc2...:b831ddf6...:小号2
```

也可以直接写进 config.json 的 `twitter.accounts` 数组（旧的单账号 `auth_token`/`ct0` 写法继续兼容，面板保存后会自动同步为 accounts 格式）。**多账号的价值**：某个账号被限流（冷却 15 分钟）或 cookie 失效（冷却 1 小时）时自动轮换到下一个，监控不中断；每个账号的会话文件独立存放在 `cookies.{N}.json`。

不想用 cookie 也可以填 `username` / `email` / `password` 用密码登录（首次登录可能需要邮箱验证码，脚本会自动保存 `cookies.json`）。开了两步验证的账号再填 `totp_secret`。密码登录是单账号兜底方案，与多账号轮换互斥。

### 3. 邮箱通知（可选，国内直连不依赖代理）

邮箱渠道在国内网络直连可达，**不依赖代理**，和 Telegram 渠道互为备份（两个都配置就同时推送，至少配一个）。

以 QQ 邮箱为例：

1. 网页登录 [mail.qq.com](https://mail.qq.com) → 设置 → 账号（账户）→ 找到「POP3/IMAP/SMTP 服务」→ 开启「IMAP/SMTP 服务」→ 按提示用手机发短信获取**授权码**（不是 QQ 密码！）。
2. 填进 config.json：
   ```json
   "email": {
     "smtp_host": "smtp.qq.com",
     "smtp_port": 465,
     "username": "你的QQ号@qq.com",
     "password": "刚才拿到的授权码",
     "to": ""
   }
   ```
   `to` 留空发给自己的话就填空字符串；想发给别人（比如另一个邮箱）就填对方地址。
3. 163 邮箱同理：`smtp_host` 改成 `smtp.163.com`，在网页版设置里开启 SMTP 拿授权码。Gmail 等国外邮箱也能用（走代理环境才行）。

配置好后跑 `.venv/Scripts/python monitor.py --test`，两个渠道会分别发一条测试消息。

### 4. Server酱（可选，推送进微信，国内直连不依赖代理）

1. 打开 [sct.ftqq.com](https://sct.ftqq.com) → 微信扫码登录 → 点击「SendKey」复制你的 Key（`SCT` 开头）。
2. 填进 config.json：
   ```json
   "serverchan": { "sendkey": "SCTxxxxxxxx你的Key" }
   ```
3. 扫码后微信里会关注服务号「**方糖**」，推送就是从这个服务号发出来的消息。
4. 免费档每天 5 条；超出会报错（日志里能看到）。想无限量可以捐 9 元/year 或改用企业微信机器人。

### 5. 其他可选渠道（未实现，需要可加）

- **企业微信群机器人**（一个 webhook URL 就行，最稳定无限量）
- **钉钉群机器人**（同上）
- **Bark**（iOS app，一个 URL 即可）
- **NapCat QQ 机器人**（真 QQ 私聊，需常驻部署 NapCat 并用小号扫码，有风控风险）

想加哪个告诉我，都是十行代码的事。

## 二、安装与运行

**推荐：直接跑 Web 面板**（监控循环 + 管理界面一体）：

```bash
cd /d/Twitter-monitoring
.venv/Scripts/python app.py        # 或直接双击 start.bat
```

然后浏览器打开 **http://127.0.0.1:8787**：

- **推送记录**：每条推文的推送时间、账号、疑似重置标签、内容/翻译/时间换算（点开行展开）、推送渠道和状态，每 30 秒自动刷新
- **配置**：可视化修改监控对象（可加多个用户名）、轮询间隔、关键词（支持 `re:` 正则）、代理、三个推送渠道的凭据、X 账号认证、面板访问密码——保存后下一轮轮询立即生效
- **运行日志**：最近 150 行实时日志
- 顶部按钮：**立即检查**（手动触发一次轮询）、**测试推送渠道**、**停止/启动监控**
- **健康标签**：顶部实时显示「接口正常/异常」「搜索正常/异常」；主监控**连续失败 3 轮**会自动向你的渠道推送告警（30 分钟冷却），恢复后再报平安——不用守着面板也知道监控死没死

默认只监听 127.0.0.1（仅本机访问）；想在局域网其他设备打开，把 config.json 里 `panel.host` 改成 `0.0.0.0`，并**务必设置 `panel.password`**。

命令行方式仍然可用（无界面、轻量）：

```bash
cd /d/Twitter-monitoring

# 首次：创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 填好 config.json 后，先测试各推送渠道
.venv/Scripts/python monitor.py --test

# 正式运行（持续前台运行，Ctrl+C 退出）
.venv/Scripts/python monitor.py
```

首次运行只记录"基准推文"不推送（避免刷屏历史推文），从第二轮开始，新推文就会推到你的 Telegram。想立刻验证整条链路，可以：

- 把 `state.json` 删掉再跑一次 `--once`，只会收到一条最新推文做基准，或者
- 临时把 `poll_interval_seconds` 改小、等 Tibo 发帖实测。

其他参数：

```bash
.venv/Scripts/python monitor.py --once             # 只轮询一次就退出（适合计划任务）
.venv/Scripts/python monitor.py --interval 60      # 临时改轮询间隔
```

### Windows 开机自启（可选）

面板版开机自启：

```bat
schtasks /Create /TN "Twitter-monitoring" /TR "D:\Twitter-monitoring\.venv\Scripts\python.exe D:\Twitter-monitoring\app.py" /SC ONSTART /RU %USERNAME%
```

或者双击 `start.bat`。

## Docker 部署（可选）

项目自带 [Dockerfile](Dockerfile) 和 [docker-compose.yml](docker-compose.yml)，容器里跑的就是同一个面板 + 监控循环。

```bash
cd /d/Twitter-monitoring
docker compose up -d --build      # 构建并后台启动
docker compose logs -f            # 看日志，Ctrl+C 退出（不影响运行）
docker compose down               # 停止
```

注意事项：

1. **代理地址要改**：容器里的 `127.0.0.1` 是容器自己，不是宿主机。Docker 部署时把 config.json 的 `proxy` 改成 `http://host.docker.internal:12450`（compose 已自动注册 host.docker.internal 指向宿主机）。
2. **数据持久化**：compose 把整个项目目录挂载进容器，config.json / records.db / cookies.json / monitor.log 都落在宿主机本目录，删容器数据不丢。
3. **端口/监听**：compose 里已设 `PANEL_HOST=0.0.0.0` 并映射 8787 端口，局域网可访问，**务必在面板里设置访问密码**。
4. 时区已设为 Asia/Shanghai，容器内日志时间与北京时间一致。

## 免费情报（X 全网关键词搜索）

不用只盯着监控的几个账号：程序会按配置的搜索语句定时搜索**全网**推文，命中「free model / free credits / 送token / 限时免费」之类的新推文立即推送到你的渠道（带【免费情报】标签），每天到点再发一条**日报**汇总过去 24 小时的全部情报（含当时推送失败的）。面板「免费情报」页可以看全部记录、手动触发搜索和日报。

- **搜索语句**在 config.json 的 `intel.queries` 或面板「配置」页配置，每行一条，支持 X 搜索语法：`lang:en` 限定语言、`min_faves:5` 限定最少点赞（过滤垃圾帖）、`-filter:replies` 排除回复、`-airdrop` 排除含某词的推文等。留空用内置默认两条（英文 + 中文）。
- X 搜索自带"语义扩展"会放进不少无关推文，所以程序搜回来后会再按**本地关键词正则**过滤一遍（`intel.must_match` 命中才收、`intel.exclude_match` 命中即丢弃，高级用户可在 config.json 里自定义，留空用内置默认；被过滤的在情报页标"未命中关键词"）。
- **搜索间隔**默认 10 分钟（最低 5），间隔越大越不容易触发 X 限流。
- **首轮只建基准不推送**：第一次启用时会把当前搜到的推文记为基准（避免历史推文一次性刷屏），之后的日报会把这些基准一并汇总给你（发过后标记"已入日报"），再之后每条新情报实时推送。
- **去重**：搜索语句之间、搜索与监控账号之间自动按推文 ID 去重，同一推文只会收到一次。
- 手动触发：面板按钮，或命令行 `.venv/Scripts/python monitor.py --search`（搜一轮）、`--digest`（立即发日报）。

## 三、配置说明（config.json）

| 字段 | 说明 |
|---|---|
| `proxy` | 本机代理地址，默认 `http://127.0.0.1:12450`（本机 12450 端口的代理已验证可用）。访问 x.com 和 api.telegram.org 都必须走它，**开机要先把代理软件跑起来** |
| `twitter.monitor` | 要监控的用户名列表（不带 @），可以加人 |
| `twitter.official` | 官方账号列表（如 `Zai_org` / `opencode` / `TencentAI_News`，即 GLM、OpenCode、腾讯 WorkBuddy 的官方公告账号）：这些账号的推文会带【官方】标签，方便一眼识别厂商公告；需同时在 monitor 列表里才会被监控 |
| `twitter.auth_token` / `ct0` | 推荐的认证方式，浏览器 cookie |
| `twitter.username/email/password` | 备选：密码登录 |
| `telegram.bot_token` / `chat_id` | 推送渠道一：Telegram（需要代理） |
| `email.smtp_host` / `smtp_port` / `username` / `password` | 推送渠道二：邮箱 SMTP（国内直连，不依赖代理；QQ/163 邮箱用授权码）。`to` 留空发给自己 |
| `email.from_name` | 邮件发件人显示名（收件箱里看到的名字），默认「Twitter monitoring」，面板可改 |
| `serverchan.sendkey` | 推送渠道三：Server酱（微信服务号推送，国内直连；免费档每天 5 条） |
| `poll_interval_seconds` | 轮询间隔，默认 90 秒。**不建议低于 60**，太频繁会触发 X 限流（触发后自动等 15 分钟） |
| `keywords` | 留空 `[]` 推送全部新推文；填了就只推命中的。普通关键词不区分大小写；**`re:` 前缀 = 正则**，例如只推重置系列：`["re:\\breset\\b", "re:^\\s*day\\s*\\d"]`（实测对他 151 条历史推文能覆盖全部 24 条 Day 系列动态 + 直接说 reset 的 22 条，零漏报） |
| `intel.enabled` | 免费情报功能开关，默认开 |
| `intel.queries` | 全网搜索语句列表，每行一条，支持 X 搜索语法，留空用默认（见上节） |
| `intel.search_interval_minutes` | 搜索间隔（分钟），默认 10，最低 5 |
| `intel.digest_time` | 每天日报发送时间（北京时间 HH:MM），默认 09:00 |
| `database.backend` | 存储后端：留空 = SQLite（默认，零配置）；可选 `mysql` / `postgres` |
| `database.host/port/user/password/name` | 远程数据库连接信息（端口 MySQL 3306 / PG 5432）；**数据库需提前创建好，表会自动建**；使用前先装驱动 `pip install PyMySQL` 或 `pip install psycopg2-binary`（requirements.txt 里已注释，取消注释亦可，Docker 部署改完需重新 build）；切换后历史记录从新后端重新积累；修改后重启生效 |

## 四、文件说明

- `app.py` Web 面板 + 监控循环（推荐入口）
- `monitor.py` 核心逻辑 / 命令行入口
- `xclient.py` X 接口适配层（twikit 补丁、事务 ID 生成、搜索 queryId 自愈都在这里，X 改版优先改它）
- `store.py` 存储层（push_records 的 SQLite / MySQL / PostgreSQL 多后端实现）
- `static/index.html` 面板页面
- `records.db` 推送记录数据库（SQLite）
- `config.json` 配置（**含账号凭据，不要外传/提交到 git**）
- `cookies.json` / `cookies.{N}.json` 登录态（后者为多账号各自的会话文件），自动生成（同上，不要外传）
- `state.json` 各用户已推送到的最新推文 ID / 显示名缓存，自动生成；删掉它会重新建立基准（不推送历史）
- `monitor.log` 运行日志（自动轮转：单文件 5MB，最多保留 3 份历史）
- `tests/test_core.py` 核心逻辑单元测试，运行 `.venv/Scripts/python tests/test_core.py` 一键回归
- `start.bat` 双击启动面板

## 五、已知风险

- X 在 2025 年改版了前端（旧的 `ondemand.s.*.js` 指纹文件已移除），twikit 2.3.3 的请求指纹生成和用户字段解析会报错。**monitor.py 内置了运行时补丁**：
  - `x-client-transaction-id` 改用 [XClientTransaction](https://pypi.org/project/XClientTransaction/)（≥1.0.3，已加入 requirements.txt）的新算法生成真实指纹——搜索接口（SearchTimeline）会严格校验该头，占位值会 404；
  - X 把用户对象切换成了无 `legacy` 的新格式，补丁会自动合成 twikit 需要的旧结构；
  - 搜索接口的 GraphQL queryId 若再遇改版 404，程序会自动从 x.com 前端 main bundle 重新提取并重试。
  若以后 twikit 发布了正式修复，可简化这些补丁。
- 非官方接口，X 改版可能导致取推文/搜索失败；届时升级 twikit（`pip install -U twikit`）或换用 [EthanC/Bluebird](https://github.com/EthanC/Bluebird) 这类多源方案。
- 监控用的 X 账号有被风控的可能，用小号、轮询别太频繁；情报搜索默认 10 分钟一轮 + 每轮 2 条语句，是刻意留的安全余量，别调太快。
- 如果部署机器在国内，需要能访问 x.com 和 api.telegram.org 的网络环境（代理，见 `proxy` 配置项）。
