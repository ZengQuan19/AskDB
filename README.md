# 问数 AskDB

> 接上你的数据库，向 AI 提问，自动查询你的数据库。
> 智能数据库查询，无需写 SQL。

[![Python](https://img.shields.io/badge/python-3.13-blue)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()
[![Version](https://img.shields.io/badge/version-0.2.0-orange)]()

## 功能

已实现：

- 中文自然语言提问，自动查询数据库（MySQL / SQLite）
- 多轮对话：自动带上最近 3 轮问答，支持「那上个月呢」这类追问；可一键重置
- 多数据源管理：在网页上新增 / 删除，配置自动落盘
- 自动读取数据库结构，交给大模型改写成紧凑格式并缓存
- 库结构自动注入提问提示词，AI 据此生成查询
- AI 工具调用：模型只能选择编码 Q1~Q4，**不能自由书写 SQL**
- 五道安全闸（见「安全」一节）
- 模型配置可在网页修改（API Key / Base URL / 模型 / 超时），保存即生效
- 生成参数向使用者开放（temperature / 最大工具轮数）
- 配置自举：首次运行自动创建 `instance/` 下的配置文件
- 管理员口令 + 敏感信息遮罩：API Key 永不回显，数据库密码默认遮罩、仅管理员可点开查看
- 三类工具：`list_tables`（看有哪些表）、`describe_table`（看列定义）、`run_query`（编码查询）
- 查询过程可视化：首页可展开查看实际执行的 SQL、行数、token 用量

### 尚未实现

以下功能**还没做**，全项目文档里出现时一律用 ~~删除线~~ 标注：

- ~~账号密码登录（管理员 / 普通用户）~~ —— 目前只有「临时管理员口令 + session」
- ~~多数据源一键切换~~ —— 现在所有已缓存结构的库会一起塞进提示词
- ~~数据库密码 / API Key 加密存储~~ —— `instance/*.json` 里目前是明文
- ~~Docker 一键部署~~
- ~~流式输出（打字机效果）~~ —— 现在是等模型全部返回再显示
- ~~图表可视化~~
- ~~回答中的 Markdown 渲染~~ —— 模型输出的表格目前显示为原始竖线
- ~~pytest 测试 / ruff lint 流水线~~
- ~~审计日志~~（记录每次工具调用：谁问的、什么编码、什么 SQL）
- ~~限流与多用户并发控制~~

## 快速开始

### 前置要求

- Python 3.13+
- MySQL 8.0+（只跑 SQLite 的话不需要，零配置）
- 一个大模型的 API（按 OpenAI 兼容协议调用，默认 DeepSeek）

### 本地运行

```bash
git clone https://github.com/ZengQuan19/AskDB
cd AskDB
python -m venv .venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt
python -m app
```

打开 <http://127.0.0.1:5000> 。

### 首次使用

1. 打开「设置」，点「进入管理员模式」（默认口令 `admin`，可用环境变量 `ADMIN_PASSWORD` 覆盖）
2. 在「模型连接」里填 API Key；如果你之前用过旧版 `llm_config.py`，启动时会自动迁移
3. 在「➕ 新增数据源」里添加数据库（MySQL 建议用只读账号）
4. 点该行的「更新结构」，让 AI 读懂表结构并缓存
5. 回到首页提问，例如「上月各城市销售额是多少」

## 配置

### 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `SECRET_KEY` | 建议 | session 签名密钥，不设置会使用开发用的兜底值 |
| `ADMIN_PASSWORD` | 否 | 管理员口令，默认 `admin`，上线前务必修改 |

生成密钥：`python -c "import secrets;print(secrets.token_urlsafe(48))"`

### 运行时配置文件（自动生成，已加入 .gitignore）

| 文件 | 内容 |
|---|---|
| `instance/config.json` | 模型连接（含 API Key）+ 生成参数 |
| `instance/db_list.json` | 数据源清单（含数据库密码） |
| `instance/schema_cache.json` | 各库的结构文本缓存 |
| `instance/app_settings.json` | 权限开关（是否允许非管理员更新结构） |

这些文件**缺失时会自动创建**，不会覆盖已有内容；配置里新增字段也会自动补默认值。

## 架构

```
浏览器
  │
  └─ app.py                      Flask 路由：首页 / 设置 / 数据源增删 / 结构更新
       ├─ agent.py               问答循环：问题 → 模型 → 工具 → 模型 → 回答
       │    ├─ llm.py            OpenAI 兼容协议调用（每次读配置，改完即生效）
       │    └─ tools.py          编码表 Q1~Q4 + 解码 + 参数化执行
       │         └─ sql_guard.py SQL 规则检查（与 AI 完全无关的一层）
       ├─ schema_store.py        结构改写与缓存（调用大模型）
       │    └─ db_operator.py    连接 / 数据源管理 / 读取结构
       │         └─ db_reader.py MySQL 与 SQLite 的信息目录统一解析
       └─ config_store.py        配置自举与读写
```

## 安全

**五道闸，任何一道不过就不执行：**

1. **编码表** —— AI 只能选 Q1（明细）/ Q2（JOIN 明细）/ Q3（聚合）/ Q4（分组聚合），没有自由写 SQL 的能力
2. **解码校验** —— 表名、列名必须真实存在于该库结构里；操作符、聚合函数走白名单
3. **参数化** —— 所有值走 `%s` / `?` 占位符，值里的引号和分号不会进入 SQL
4. **规则检查** —— 单句、SELECT 开头、禁注释、禁 DROP/DELETE 等 40+ 关键字、禁 `SELECT *`、表名白名单、强制 LIMIT
5. **只读连接** —— SQLite 用 `mode=ro`，MySQL 执行 `SET SESSION TRANSACTION READ ONLY`

**部署提醒：**

- 目标库请使用**只读账号**（只授 `SELECT`），这是最后一道物理防线
- `instance/` 里有明文密码，**不要提交、不要共享**
- 不要直接暴露到公网；如果必须，前面套 HTTPS 反向代理并改掉默认口令

## 排错

| 现象 | 原因 | 解决 |
|---|---|---|
| 提问后提示「还没有配置 API Key」 | 模型没配 | 设置 → 模型连接，填 Key 保存 |
| 提问后说「没有可用的数据库结构」 | 没生成结构缓存 | 设置 → 对应数据源 → 更新结构 |
| 「测试连接」报 1045 | 账号或密码不对 | 核对账号密码，确认库已授权 |
| 页面显示的是旧内容 | Flask 模板缓存 | 重启 `python -m app` |
| 工具报「表 xxx 不存在」 | 结构缓存过期 | 点「更新结构」后重试 |

## License

MIT
