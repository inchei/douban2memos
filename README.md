<p align="center">
  <img src="logo.png" width="120" alt="douban2memos">
</p>

# douban2memos

![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-3776AB)
[![Memos](https://img.shields.io/badge/Memos-%E2%89%A50.26.0-1E6D51)](https://github.com/usememos/memos)

把豆瓣「看过 / 读过 / 听过 / 玩过」且**带短评**的收藏导入 [Memos](https://github.com/usememos/memos)，
memo 为纯文字，正文含状态词、条目名、短评、可选评分和豆瓣条目链接。

## 版本要求

| 依赖 | 版本 |
| --- | --- |
| Python | ≥ 3.11 |
| Memos | API 模式 ≥ 0.26.0；直写库 ≥ 0.22 |

## 原理

- 豆瓣没有公开 API，数据源沿用 douban-backup 的做法：日常抓公开 RSS
  `https://www.douban.com/feed/people/{uid}/interests`，初始用油猴脚本导出的 CSV 导入
- 仅导入**带短评**（RSS 的 `备注:` / CSV 的「我的短评」）且状态为看过/读过/听过/玩过的条目，
  无短评或想看/在读等非完成态一律跳过
- memo 正文：`读过《卡拉马佐夫兄弟》：……〔力荐〕` + 豆瓣条目链接
- 每条 memo 以 `uid = douban-{条目id}` 幂等，重复运行不产生重复 memo
- 时间用 RSS 的 pubDate / CSV 的打分日期写入 `createTime`/`created_ts`，保留原始时间
- 标签 `--tag` 默认以 `#tag` 追加到正文并同时显式传入（API: `tags`，直写库: `payload.tags`，双写确保标签生效）；
  可用 `--no-tag-in-content` 关闭正文追加，此时仅显式传入标签，正文不含 `#tag`，编辑 memo 后标签会丢失
- 列表按时间降序，状态文件记录最新时间，下次运行提前停止处理更旧条目；`--full` 强制全量

## 使用方式

### 初始导入历史收藏（一次性）

安装 douban-backup 的油猴脚本 <https://greasyfork.org/en/scripts/420999>，打开自己豆瓣主页，
点「导出看过的片 / 读过的书 / 听过的碟 / 玩过的游戏」，脚本会逐页导出各类型 CSV
（`db-movie-20260816.csv` 等）。把导出的 CSV 放进**脚本同目录**，直接运行即可自动检测导入
（API 或直写均可，直写需先停止 memos）：

```sh
python3 douban2memos.py --api http://localhost:5230 --user admin --password '你的密码'
```

（也可用 `--import-csv 文件1,文件2` 指定其它路径。）导入后写入 `state.json` 水印，
后续 RSS 增量会跳过历史老条目。

### 方式一：API 模式

memos >= 0.30（登录换取短期 token）：

```sh
python3 douban2memos.py --douban-user-id 你的豆瓣ID \
    --api http://localhost:5230 --user admin --password '你的密码'
```

 0.26.0 ≤ memos < 0.30（使用账号里的 Access Token）：

```sh
python3 douban2memos.py --douban-user-id 你的豆瓣ID \
    --api http://localhost:5230 --token 'AccessToken'
```

### 方式二：直写数据库

需停止当前 memos，写完后重启。

```sh
python3 douban2memos.py --douban-user-id 你的豆瓣ID --db ~/.memos/memos.db --user admin
```

## 同步

### cron

```sh
# 每 30 分钟同步一次
*/30 * * * * cd /path/to/douban2memos && python3 douban2memos.py --config config.toml >> sync.log 2>&1
```

### GitHub Actions

前提：memos 实例可从公网访问。使用 Cloudflare 时可出现 1010 错误，可选择配置 Security Rules 豁免

```
(http.host eq "你的公网域名" and starts_with(http.request.uri.path, "/api/v1/memos"))
```

的 Browser Integrity Check 等方法使 Action 可访问。

fork 本仓库，参考 [sync.yml](.github/workflows/sync.yml) 每 6 小时在 GitHub runner 上自动跑一次 API 模式同步。

配置仓库 Secrets / Variables（Settings → Secrets and variables → Actions）：

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `DOUBAN_USER_ID` | Secret | 豆瓣用户 ID（必填） |
| `MEMOS_API` | Secret | memos 地址，如 `https://memos.example.com`（必填） |
| `MEMOS_PASSWORD` | Secret | memos 密码（memos ≥ 0.30，推荐） |
| `MEMOS_USER` | Secret | memos 登录用户名（配合密码） |
| `MEMOS_TOKEN` | Secret | 或 memos < 0.30 的 Access Token（替代密码） |
| `MEMOS_VISIBILITY` | Secret / Variable | memo 可见性：`private` / `protected` / `public`（可选，默认 `private`；可用 Variables，更语义化） |
| `MEMOS_TAG` | Secret / Variable | 附加标签（可选，空 = 不加；如 `douban` 则默认正文追加 `#douban`，`--no-tag-in-content` 可关闭） |

`MEMOS_VISIBILITY` / `MEMOS_TAG` 同时对定时任务（`schedule`）与手动触发（`workflow_dispatch`）生效（Secrets 优先于 Variables，未配置则默认 `private` / 不加标签）。

可在首次本地全量导入后，用 **workflow_dispatch** 手动触发一次，在 `watermark` 输入框填本地
`state.json` 的 `last_updated_ts`（epoch 秒），避免重复全量初始化。

## 卸载

删除 uid 以 `douban-` 开头（即本工具导入）的 memo，并重置增量状态文件，增加 `--delete` 参数即可，例：

```sh
python3 douban2memos.py --delete --api http://localhost:5230 --user admin --password '你的密码'
```

## 配置文件

默认读取当前目录 `config.toml`，也可用 `--config` 指定其它路径；

命令行参数会覆盖配置文件。参考 `config.example.toml`。

## 说明与限制

- **RSS 只保留最近 10 条**：短时间集中标记过多时中间条目可能漏导，建议缩短同步周期或集中标记后
  手动触发一次；历史收藏用油猴 CSV 一次性补齐
- 豆瓣无官方 API，公开 RSS 与网页抓取均为非官方手段，未来可能失效
- 已导入条目后续改短评不会自动更新（`uid` 不变即视为已导入）；如需修正可 `--delete` 后重导
- 直写数据库前请停止 memos，否则可能 `database is locked`
- 标签默认同时写入正文与显式标签字段（双写确保标签生效），Memos 前端编辑时会按正文重新提取标签；若用 `--no-tag-in-content` 关闭正文追加，仅显式传入标签（API: `tags`，直写库: `payload.tags`），再次编辑后会丢失

## 许可证

[GNU General Public License v3.0 or later](LICENSE)（GPL-3.0-or-later）