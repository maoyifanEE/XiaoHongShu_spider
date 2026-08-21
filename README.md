# 小红书指定博主公开信息采集与 Excel 导出工具

版本：`0.1.0`

这是一个低频、串行、只读的数据整理工具，用项目专属 `browser_profile/` 保存登录状态。它不会读取你的日常 Chrome/Edge Profile，不要求你提供 Cookie、Token 或密码，也不包含验证码破解、指纹伪装、代理池或绕过风控的代码。

## 日常使用

双击 `start.bat`。脚本会自动创建 `.venv`、安装依赖、安装项目内 Playwright Chromium、检查配置、启动采集、写入 SQLite、导出 Excel 并执行离线 QA。

首次运行如果未登录，会打开一个可见浏览器窗口并提示：

```text
首次运行需要登录小红书，请在当前浏览器窗口完成登录。
完成后无需关闭浏览器，程序会自动检测。
```

完成扫码、短信或安全验证后，不需要关闭浏览器，程序会自动继续。后续正常运行会复用 `browser_profile/`。

## 目录

- `config/config.yaml`：博主、浏览器、采集和安全配置。
- `browser_profile/`：项目专属浏览器登录状态，敏感目录，不进入 Git。
- `data/xhs_data.sqlite3`：长期 SQLite 数据库。
- `data/raw/`：已清理敏感字段的结构化原始 JSON。
- `data/checkpoints/`：断点续跑状态。
- `logs/`：运行日志。
- `output/`：Excel 输出。
- `screenshots/errors/`：仅异常、QA 或安全状态截图。

## 配置其他博主

编辑 `config/config.yaml` 的 `creators`，新增或修改：

```yaml
creators:
  - name: "昵称"
    xhs_id: "小红书号"
    user_id: "页面 user_id"
    url: "https://www.xiaohongshu.com/user/profile/页面user_id"
    enabled: true
```

不用修改 Python 文件。

## CLI

在项目目录中运行：

```powershell
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode smoke
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode export-only
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode qa-only
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode debug-extract --note-id 664c92e5000000001500804e
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode golden-live
.\.venv\Scripts\python.exe -m xhs_profile_exporter --creator 辣香郭 --max-notes 5
```

模式说明：

- `collect`：默认采集，访问小红书。
- `smoke`：只采配置中的少量笔记，用于首轮验证。
- `login-only`：只验证和保存登录状态。
- `export-only`：只从 SQLite 重新生成 Excel，不访问小红书。
- `qa-only`：只做本地数据校验，不访问小红书。
- `debug-extract`：对指定 `--note-id` 生成单帖字段核对报告，输出到 `debug/live_extract/<run_id>/extraction_report.json`，用于开发阶段人工审查字段来源和 DOM 证据。
- `golden-live`：只验证 `tests/fixtures/golden_notes/` 中的固定笔记，使用当前生产 extractor 的 live 结果直接对比人工 fixture，并输出 `validation/golden_review/<note_id>/` 审查材料。

Golden fixture 字段使用 `exact`、`missing`、`skip` 三种断言语义；`skip` 字段会记录 live actual/source，但不参与 PASS/FAIL。

## 数据质量规则

- `NULL` 表示未知或未返回，绝不伪装成 `0`。
- `999` 这类完整数字标记为精确。
- `1.2万`、`3.2w` 这类 UI 缩写会转为近似值，并标记为非精确。
- Excel 每次从 SQLite 生成，不作为增量数据源。
- 评论保存当前页面默认排序下实际显示的前三条一级评论，并记录采集时间和排序模式。

## 安全停止

遇到 CAPTCHA、滑块、短信验证、二次验证、安全验证时，程序保存 checkpoint 并等待人工处理。遇到访问频繁、环境异常、风险提示等信号时，程序保存当前进度并安全停止。

可以用 `Ctrl+C` 停止。程序会尽量提交 SQLite transaction、写 checkpoint、记录日志并关闭浏览器。

## 常见限制

平台页面结构、评论排序、互动数和可见笔记都可能随时间、账号和算法变化。最终报告表述为“本次运行发现的全部当前公开可访问笔记”，不声称覆盖账号历史上绝对全部笔记。
