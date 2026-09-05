# 贡献指南

欢迎为 douban2memos 贡献代码、文档与 issue。项目以 [GPL-3.0-or-later](LICENSE) 许可发布。

## 开发环境

| 工具 | 版本 |
| --- | --- |
| Python | ≥ 3.11（标准库即可，无第三方依赖） |

## 常用命令

```sh
python3 -m py_compile douban2memos.py       # 语法检查
python3 douban2memos.py --help              # 参数一览
python3 douban2memos.py --douban-user-id inchei --dry-run   # 预览
python3 douban2memos.py --import-csv db-movie.csv --api …   # 初始导入
```

## 代码约定

- 遵循 [AGENTS.md](AGENTS.md)：单文件结构、两种写入模式、配置键名规则、增量水印逻辑、
  GPL 版权头等
- 只用 Python 标准库，禁止新增 pip 依赖
- 面向用户的输出与参数说明用中文
- 代码不加注释，只保留 docstring / 函数签名注释
- 提交前必须通过：`py_compile` 语法检查，并跑通一次 dry-run 与一次真实导入（含幂等复查）

## 提交流程

1. 从 `main` 新建分支：`git checkout -b fix/short-description`
2. 修改并自检（`python3 -m py_compile douban2memos.py` + dry-run + 真实导入）
3. 提交（改动前已按上步自检）
4. 推送并创建 Pull Request，描述改动动机与验证结果

## Issue 报告

请包含：运行环境（Python / memos 版本）、复现步骤、`config.toml`（隐去密码与 token）、
以及实际输出与预期差异。

## 许可证

新增文件请保留文件头的 `Copyright (C) 2026 douban2memos contributors` 与
`SPDX-License-Identifier: GPL-3.0-or-later` 两行。整个项目以 GPL-3.0-or-later 发布。