# Meng Anti-Spam Bot

Telegram 私聊防垃圾转发机器人。用户先发消息给机器人，机器人做基础安全检查和 LLM 垃圾校验，通过后再转发给管理员；管理员可以通过按钮直接回复用户。

## 功能

- 文本、图片、贴图、视频、文件转发给管理员。
- 文本、图片 caption、贴图 emoji 会先通过 LLM 垃圾校验。
- 视频和文件不走 LLM 校验，避免大文件或长媒体拖慢流程。
- 未通过 LLM 校验时，直接提示用户修改后再发送，不转发给管理员。
- 第一次联系只能发送文本，通过后会写入允许用户表。
- MySQL 持久化用户信息和消息历史。
- 媒体消息只保存元数据，不保存文件本体。
- 管理员点击“回复”按钮后，可以向原用户发送文本、图片、视频、贴图、文件等回复。
- 内置 10 秒发送间隔限制，降低刷屏。

## 项目文件

```text
bot.py                         主程序
requirements.txt               Python 依赖
config.env_example             环境变量示例
meng_antiSpamBot.service       systemd 服务示例
```

## 环境要求

- Python 3.11+
- MySQL
- Telegram Bot Token
- OpenAI-compatible Chat Completions API

安装依赖：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 配置

复制示例配置：

```bash
cp config.env_example config.env
```

配置项：

```env
TOKEN=机器人令牌
OWNER_ID=接收转发消息的管理员 Telegram user id
MYSQL_HOST=localhost
MYSQL_USER=数据库用户名
MYSQL_PASSWORD=数据库密码
MYSQL_DATABASE=telegram_bot
LLM_API_KEY=大模型 API Key
LLM_API_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

可选配置：

```env
LLM_API_URL=https://api.openai.com/v1/chat/completions
LLM_TIMEOUT=15
TELEGRAM_CONNECT_TIMEOUT=30
TELEGRAM_READ_TIMEOUT=60
TELEGRAM_WRITE_TIMEOUT=30
TELEGRAM_POOL_TIMEOUT=30
```

说明：

- `LLM_API_KEY` 也可以用 `OPENAI_API_KEY` 代替。
- `LLM_API_BASE_URL` 需要是 OpenAI-compatible API 的 `/v1` 根路径。
- 如果填写了 `LLM_API_URL`，程序会优先使用它。
- 如果 LLM key 缺失或 LLM 调用失败，机器人不会绕过审核转发，会提示用户稍后再试。

## 数据库

程序启动时会自动创建和升级表结构。

`allowed_users` 保存已允许用户：

- `user_id`
- `username`
- `first_name`
- `last_name`
- `created_at`

`message_history` 保存消息历史和媒体元数据：

- `user_id`
- `username`
- `message_type`
- `message_text`
- `file_id`
- `file_unique_id`
- `caption`
- `file_name`
- `mime_type`
- `file_size`
- `width`
- `height`
- `duration`
- `emoji`
- `created_at`

历史查询优先使用当前或已知的 `username`，查不到时用 `user_id` 兜底。媒体文件本体不入库，`file_id` 可用于同一个 bot 重新发送该文件。

## 运行

本地前台运行：

```bash
source venv/bin/activate
set -a
source config.env
set +a
python bot.py
```

## systemd 部署

服务文件默认使用：

```text
/opt/meng_antiSpamBot
```

部署示例：

```bash
mkdir -p /opt/meng_antiSpamBot
cp -r bot.py requirements.txt config.env meng_antiSpamBot.service /opt/meng_antiSpamBot/
cd /opt/meng_antiSpamBot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp meng_antiSpamBot.service /etc/systemd/system/meng_antiSpamBot.service
systemctl daemon-reload
systemctl enable meng_antiSpamBot
systemctl restart meng_antiSpamBot
```

查看状态：

```bash
systemctl status meng_antiSpamBot
```

查看日志：

```bash
tail -n 100 -f /var/log/telegram_bots/meng_antiSpamBot.log
```

如果日志目录不存在：

```bash
mkdir -p /var/log/telegram_bots
```

## 消息处理流程

1. 用户发送消息给机器人。
2. 未授权用户第一次只能发送文本。
3. 文本会先做 HTML 安全过滤和关键词拦截。
4. 文本、图片 caption、贴图 emoji 调用 LLM 判断是否垃圾信息。
5. 未通过审核时，回复用户“检测到您的消息中可能包含垃圾信息，请修改后再发送。”
6. 通过审核后，写入用户记录和消息历史。
7. 消息转发给 `OWNER_ID`。
8. 管理员点击回复按钮，进入回复会话。

## 常见问题

### `telegram.error.NetworkError: httpx.ConnectError`

这是服务器连接 Telegram API 失败，通常不是代码逻辑错误。检查服务器是否能访问 Telegram：

```bash
curl https://api.telegram.org
```

如果服务器网络环境无法直接连接 Telegram，需要配置系统网络或代理。

### `tail: cannot open '100'`

命令写错了。应该使用：

```bash
tail -n 100 -f /var/log/telegram_bots/meng_antiSpamBot.log
```

### LLM 返回 ```json 代码块能不能解析

可以。程序会从 LLM 返回内容中提取 `{...}` JSON 对象，再读取 `is_spam` 字段。

### `file_id` 怎么用

`file_id` 可以让同一个 bot 复用 Telegram 文件，不需要重新上传。例如：

```python
await context.bot.send_photo(chat_id=user_id, photo=file_id)
await context.bot.send_document(chat_id=user_id, document=file_id)
```

`file_unique_id` 只能用于识别同一个文件，不能直接发送或下载。
