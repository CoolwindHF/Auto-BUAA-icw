# Auto-BUAA-icw

> 🎓💰 北航「网上财务综合信息系统」学生收入自动监控 —— 助学金 / 助研津贴 / 劳务费一到账，邮件第一时间通知你，再也不用天天登录查窝囊费了。

![Python](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat)
![Dependencies](https://img.shields.io/badge/dependencies-requests%20%7C%20pyyaml-informational)
![Package Manager](https://img.shields.io/badge/managed%20by-uv-de5c77?logo=uv)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20OpenWrt-lightgrey)

## ✨ 核心特性

- **🔔 新到账即时邮件通知** — 以流水号(`lsh`)为唯一标识做增量对比，只在有新收入录入时发送该新增部分，绝不重复打扰
- **💾 本地持久化状态** — 已见流水号落盘 `state.json`，进程重启 / 服务拉起都不会重发全量邮件;初次部署自动发一封基线邮件顺带验证 SMTP 配置
- **🔁 默认常驻守护** — 无需 cron，直接运行即进入轮询循环，间隔在 `config.yaml` 秒级可调;单轮登录 / 网络 / 发信异常只记日志自动重试，进程不死
- **🛡️ 通知不丢失** — 邮件发送成功才更新状态，SMTP 偶发失败时下一轮自动重试，新收入通知不会悄悄丢掉
- **🧩 零框架轻依赖** — 仅 `requests` + `pyyaml`，SSO 登录 → 票据跳转 → JWT 换取 → API 分页拉取全链路纯 HTTP 实现，uv 管理环境开箱即用

## 🚀 快速开始

### 前置要求

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/)(推荐)或任意包管理器
- 一个可用的 SMTP 邮箱(QQ / 163 / Gmail 等，需开通 SMTP 并生成授权码)
- 北航统一身份认证账号(有工资 / 助学金 / 劳务收入的学生)

### 安装

```bash
git clone <repository-url> && cd Auto-BUAA-icw

uv sync                              # 创建 .venv 并安装依赖
cp config_example.yaml config.yaml   # 复制配置模板
chmod 600 config.yaml                # 配置含密码，收紧权限
```

### 使用

```bash
# 1️⃣ 先 dry-run 验证登录与抓取是否正常(不发邮件、不改状态)
uv run main.py --dry-run

# 2️⃣ 确认无误后常驻运行(默认行为)，Ctrl-C 退出
uv run main.py

# 3️⃣ (可选)只跑一轮，交给外部调度器管理
uv run main.py --once
```

常驻模式日志同时输出到控制台与 `logs/check.log`(1MB × 5 自动轮转)。
持久化部署任选 launchd / systemd / procd / supervisord，进程异常退出由服务管理器拉起，停止时发 `SIGTERM` 即可优雅退出。

## ⚙️ 配置说明

所有配置集中在 `config.yaml`(完整模板见 [`config_example.yaml`](config_example.yaml)):

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `student.username` | str | — | 统一身份认证账号(必填) |
| `student.password` | str | — | 统一身份认证密码(必填) |
| `check_interval` | int | `1800` | 监控间隔(秒)，30 分钟 |
| `query.limit` | int | `100` | 单页拉取条数，超出自动翻页 |
| `notify_on_first_run` | bool | `true` | 初次部署是否发送全量基线邮件 |
| `mail.smtp_host` | str | — | SMTP 服务器，如 `smtp.qq.com` |
| `mail.smtp_port` | int | `465` | SSL 用 465;STARTTLS 用 587 并配 `use_starttls: true` |
| `mail.username` / `mail.password` | str | — | SMTP 授权码(**不是**邮箱登录密码) |
| `mail.to` | str \| list | — | 收件人，支持一个或多个 |

命令行参数:

| 参数 | 说明 |
| --- | --- |
| *(无)* | 常驻监控(默认) |
| `--once` | 只查询一轮后退出 |
| `--dry-run` | 只查询打印，不发邮件不改状态 |
| `--reset` | 清空本地已见记录，下次运行重新部署基线 |

> 📝 配置在启动时加载，修改 `check_interval` 或邮箱配置后需重启进程生效。

## 🤝 Contributing

欢迎 Issue 与 PR:

- 提交 Issue 请附上 `logs/check.log` 中的报错片段(注意抹掉个人信息)
- PR 请保持零新增第三方依赖的风格，并在描述中注明测试方式

## 📄 License

本项目基于 [MIT License](LICENSE) 开源。

---

<p align="center">祝各位窝囊费按时到账，一分不少 💸</p>
