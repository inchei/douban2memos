# agents.md 开发指引

本文件供 AI 助手 / 开发者在 memos-plugin-douban 仓库工作时参考。

## 项目概述

把豆瓣「看过/读过/听过/玩过」且带短评的收藏导入 Memos，纯文字 memo。
支持两种写入模式：

- **API 模式**（`--api`，memos 运行中，推荐）：memos ≥ 0.30 用 `--user`+`--password`
  调用 `/api/v1/auth/signin` 换取短期 token；memos < 0.30 用 `--token`（Access Token）。
  `POST /api/v1/memos?memoId={uid}` 设置 uid 且幂等；请求体 `createTime` 保留豆瓣时间。
- **直写数据库**（`--db`）：直接插入 memos sqlite `memo` 表（需先停止 memos）。

豆瓣无公开 API，数据源沿用 douban-backup 的做法、全程无需登录：
- 日常增量：公开 RSS `https://www.douban.com/feed/people/{uid}/interests`
  （只保留最近 ~10 条兴趣，含想看/在* 等完成态之外的状态）
- 初始全量：douban-backup 的油猴脚本（greasyfork.org/en/scripts/420999）导出的 CSV，
  自动扫描当前目录中 `db-{类型}-{yyyymmdd}.csv` 即时导入；也可用 `--import-csv` 显式指定

单文件、纯 Python 标准库实现（`urllib` / `tomllib` / `sqlite3` / `xml.etree` / `csv`），
无第三方依赖，Python ≥ 3.11（`tomllib` 3.11 才进入标准库）。不需要任何工具链或构建步骤。

## 目录结构

- `memos-plugin-douban.py`  全部代码（含文件头 GPL 版权声明）
- `config.example.toml`     配置模板
- `.github/workflows/sync.yml` 每 6 小时在 GitHub runner 上跑一次 API 模式同步
  （需 memos 公网可达；凭据走 Secrets；`state.json` 走 Actions cache 不进仓库，支持
  workflow_dispatch 传 `watermark` 播种初始水印 / `full` 强制全量；未配置 secrets 则安全跳过）
- `logo.png`   README 顶部展示的仓库 logo
- `README.md` / `AGENTS.md` / `CONTRIBUTING.md` / `LICENSE` / `.gitignore`

## 技术约束（务必遵守）

- 只用 Python 标准库，禁止新增依赖（pip 包）——同兄弟项目 memos-plugin-bangumi 的工程原则
- 配置键名 = 命令行参数去 `--`（`-` 可写作 `_`）；命令行参数优先于配置文件（argparse 默认值来自配置）
- 面向用户的输出与参数说明用中文；代码不加注释，只保留 docstring / 函数签名注释
- 修改源码时勿删文件头的版权/SPDX 行
- 保持单文件：不要拆成多文件；无 `--version`（版本无发布语义）
- 无 git hooks / CI 门禁：改完靠下面的「验证」手工自检

## 关键逻辑（修改时保持）

- 数据源有两路，最终都归一为同构 dict（uid/content/create_time/ts/title）：
  - RSS `fetch_rss_items` + `rss_item_to_memo`：解析 item 的 title/link/pubDate/description。
    title 带状态前缀（`^(?:最近)?(想看|看过|想读|读过|…|玩过|在玩)`），仅接受
    COMPLETED_PREFIXES（看过/读过/听过/玩过）；description 的 HTML 表格里提取
    `<p>推荐: 力荐</p>`（评分）与 `<p>备注: xxx</p>`（短评）；pubDate 用 RFC822 解析为 epoch
  - CSV `read_csv_items` + `csv_row_to_memo`：`csv` 模块按行、表头可省略，
    列序 fixed：`[0]=标题 [1]=个人评分(数字1-5) [2]=打分日期(YYYY/M/D) [3]=我的短评 [-1]=条目链接`。
    `has_detail = len(row) >= 5`；状态词按链接域名推断（`category_from_link` + CATEGORY_LABELS）
  - 初始导入自动检测：`sync` 里 `import_csv` 为空时调用 `find_import_csv`，扫描当前目录中
    匹配 `db-(book|movie|music|game|drama)-\d{8}.csv` 的文件（油猴固定命名）自动导入；
    `--import-csv file1,file2` 仍可显式指定
- `status_label`：CSV 按条目链接域名给完成态文案：book=读过、music=听过、game=玩过、movie/drama=看过；
  RSS 直接用标题前缀作状态词（看过/读过/听过/玩过）
- `build_content`：`读过《标题》：短评 〔力荐〕\n\n{豆瓣条目链接}`（评分可缺省则省略括注）
- `parse_pubdate`：RFC822 转 `datetime`（补 UTC 时区）；`parse_rating_date`：YYYY/M/D 转本地零点日期
- 过滤：非完成态前缀、无短评、subject_id 提取失败，一律跳过
- `uid = douban-{subject_id}`，subject_id 由链接里的 `(?:subject|game|drama)/(\d+)` 提取
  （CSV 与 RSS 同一算法 → 跨源幂等，CSV 导入过的条目 RSS 再出现会跳过）
- 评分：RSS 是中文词（RATING_RSS），CSV 是数字（RATING_NUM + RATING_ORDER），
  统一输出中文括注 `〔力荐〕` 式样（力荐5/推荐4/还行3/较差2/很差1）
- 输出：默认只打印进度与汇总，`--verbose` 才逐条输出创建的 memo；`--dry-run` 始终打印预览内容
- 卸载：`--delete` 删除 uid 以 `douban-` 开头的 memo 并重置状态文件（不访问豆瓣，只需 memos 连接参数；
  API 模式 `DELETE /api/v1/memos/{uid}`，404 视为成功；直写库按 `uid + creator_id` 删除）
- 增量：`state.json` 记 `last_updated_ts`（epoch 秒）；items 按 ts 降序，遇到 `ts <= 水印` 即
  `break`（后续更旧）；`--full` 或首次运行（水印为 0）走全量；`--dry-run` 不读/写 memos，也不保存状态；
  GitHub Actions 模式的 `state.json` 走 cache 不进仓库，首次可用 sync.yml 的 `watermark` 输入播种，
  此后每次跑完自动存回
- API 模式幂等：`memoId` 重复时 memos 返回 `code=6`（ALREADY_EXISTS）视为跳过
- API 模式 `tag` 以 `#tag` 拼入正文（memos 标签从正文 hashtag 提取）；直写库写 `payload.tags`
- 直写库要求库已由 memos 初始化（有 `user` 表且存在用户），导入前 memos 必须停止
- 测试时可将 RSS_FEED_BASE 用环境变量 `MEMOS_PLUGIN_DOUBAN_FEED_BASE` 覆盖为本机 mock

## 验证

```sh
# 语法检查
python3 -m py_compile memos-plugin-douban.py

# 帮助
python3 memos-plugin-douban.py --help

# dry-run 预览（不写入 memos，不保存状态）
python3 memos-plugin-douban.py --douban-user-id inchei --dry-run

# API 模式端到端：本地起一个测试 memos（--data 临时目录、--port 5230）并建用户，
# 真实导入一次，再次运行确认幂等跳过，并抽查 memo 的 uid / createTime / content。
# 直写库模式：memos 停止后用 --db 指向测试库导入，重启后用 API 抽查。
```

改动后必须跑通一次 dry-run 和一次真实导入（含重复运行幂等检查）。