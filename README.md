# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

后端测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 | 规则编号、包含模式、排除模式 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 增量水位迁移（incrementmark）

旧版增量水位以无类型字符串保存。迁移到新版格式（`v2`：带类型 + 回退水位快照）时遵循以下口径：

- **类型识别不猜测**：只识别整数、小数、日期（`YYYY-MM-DD`）、日期时间（ISO 8601）四种明确格式；
  其它值（如游标 `cur_9f3a1b`）一律进入待人工处理清单，人工指定明确类型后才推进。
- **一条水位只有一个迁移版本号**：重复迁移直接返回既有结果；批量迁移按采集顺序逐条落库，
  中断后重跑只继续未迁移条目，已迁移记录不会被改回字符串。
- **迁移期间写入必须显式声明版本**：登记接口要求 `水位版本` 为 `v1`（旧字符串）或 `v2`（需带 `水位类型`）。
- **回退水位快照**：每次迁移保留原字符串水位、转换后水位、类型、采集顺序、迁移时间与操作人，
  可逐项对照输入审计（`/api/incrementmark/migration/verify`）。
- **快照登记幂等**：同一迁移版本号重复提交只生效一次。
- **同一源的新旧版本冲突**：审计视图按迁移时间排出两个版本的先后，续采采用较晚的已转换版本。
- **续采兼容**：采集执行侧清单中，已迁移水位按新版消费，未迁移水位仍以原字符串只读续采；
  迁移与同步都会回写 `crawljob`（抓取作业）清单。

迁移相关接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/incrementmark/migration/preflight` | 核对依赖、数据库环境、部署版本、本地开发数据兼容性 |
| `GET /api/incrementmark/migration/preview?entry_id=` | 预览识别类型与转换结果，不写数据 |
| `POST /api/incrementmark/migration/run` | 批量迁移（`dry_run`、`limit`、`entry_ids`），已迁移条目跳过 |
| `POST /api/incrementmark/{id}/migration` | 单条迁移，无法识别时转入待人工处理 |
| `GET /api/incrementmark/migration/manual-queue` | 待人工处理清单 |
| `POST /api/incrementmark/{id}/manual-confirm` | 人工指定明确类型后推进迁移 |
| `GET /api/incrementmark/migration/audit` | 审计视图：原字符串水位 / 新值 / 采集顺序 / 迁移时间 |
| `GET/POST /api/incrementmark/migration/snapshots` | 可追溯水位快照清单与幂等登记 |
| `GET /api/incrementmark/migration/verify` | 快照与输入逐项核对 |
| `GET /api/incrementmark/migration/crawl-manifest` | 采集执行侧清单 |
| `POST /api/incrementmark/migration/crawl-sync` | 回写采集执行侧（抓取作业）清单 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
