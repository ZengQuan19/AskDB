# 问数 AskDB

## 一切都尚未实现

### 欠账:llm_config网页设置，temperature网页设置，AI可调用工具，

> 接上你的数据库，向AI提问，自动查询你的数据库。  
> 智能数据库查询，无需写 SQL。

[![Python](https://img.shields.io/badge/python-3.13-blue)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()

## 功能

- 中文自然语言提问，自动查询MySQL。
- 支持多数据库源配置与一键切换
- 自动读取数据库结构，支持人工补充业务口径。
- 账号密码登录，管理员/普通用户
- 只读安全闸：单句SELECT，表名白名单，自动LIMIT
- Docker一键部署

### ~~-快速开始-~~
#### 待补充

### 前置要求
- Python 3.13+
- MySQL 8.0+
- ~~-一个大模型的API-~~  目前仅支持DeepSeek

### 本地运行
``` bash 
git clone https://github.com/ZengQuan19/AskDB
cd AskDB
python -m venv.venv
.venv\Scripts\activate      #Windows
source .venv/bin/activate   #Linusx/macOS
pip install -r requirements.txt

cp .env.example .env            # 然后编辑 .env，填入 SECRET_KEY 等
python -m app
```

打开 http://127.0.0.1:8000 ，默认管理员账号见「初始化管理员」一节。

### Docker 一键部署

```bash
cp .env.example .env
# 编辑 .env：设置 SECRET_KEY、ADMIN_PASSWORD、目标库连接
docker compose up -d
```

打开 http://服务器IP:8000 。

## 配置说明

| 变量 | 必填 | 说明 | 示例 |
|---|---|---|---|
| `SECRET_KEY` | 是 | session 签名密钥 | 随机 48 位字符串 |
| `ADMIN_PASSWORD` | 首次启动 | 初始管理员密码 | 强密码 |
| `DATABASE_URL` | 否 | 应用元数据库，默认 SQLite | `sqlite:///instance/app.db` |
| `LOG_LEVEL` | 否 | 日志级别 | `INFO` |

生成密钥：`python -c "import secrets;print(secrets.token_urlsafe(48))"`

## 架构

```
浏览器 → Flask（蓝本：main/auth/chat/admin）
          ├─ 应用元数据 SQLite
          ├─ 目标 MySQL（可配置、密码加密存储）
          └─ DeepSeek API
```

```
askdb/
├─ app/            # 应用（工厂 + 蓝本）
├─ deploy/         # Dockerfile / compose / 部署脚本
├─ tests/          # pytest
└─ docs/           # 截图、架构说明
```

## 开发

```bash
pip install -r requirements-dev.txt
pytest -q            # 测试
ruff check .         # lint
```

## 安全

- 数据源密码 Fernet 加密存储
- 只读 SQL 闸：白名单 + 禁写 + 自动 LIMIT
- 生产必须：HTTPS、强 `SECRET_KEY`、只读数据库账号

## 排错

| 现象 | 原因 | 解决 |
|---|---|---|
| 启动报 `SECRET_KEY` 缺失 | 没配 `.env` | 复制 `.env.example` |
| 登录后立刻掉线 | 多实例密钥不一致 | 统一 `SECRET_KEY` |

## Roadmap

- [x] 账号登录
- [x] 多数据源配置
- [ ] 流式回答
- [ ] 图表可视化

## License

MIT