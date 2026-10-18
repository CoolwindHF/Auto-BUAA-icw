#!/usr/bin/env python3
"""北航网上财务系统(icw)学生收入自动监控脚本(默认常驻)。

流程: SSO 登录 -> icw 主站 -> jump.aspx 跳转查询系统换取 Cypt 票据
      -> api-zhcx/login 换 JWT -> getStudentSRCX 拉取收入流水。
按流水号(lsh)与本地持久化状态(state.json)对比,重启不会重复通知:
  - 初次部署(state.json 不存在):把全部存量流水发一封基线邮件
    (notify_on_first_run: false 可改为只建基线不发邮件);
  - 之后每轮检查:只在出现新流水号时发送该新增部分;
  - 邮件发送成功才更新 state.json,失败则下一轮自动重试。

默认进入常驻循环,按 config.yaml 的 check_interval(秒)间隔轮询,
日志同时输出到控制台与 logs/check.log(按大小轮转);Ctrl-C / SIGTERM 优雅退出。
单轮检查中的登录、网络、发信等异常只会记日志休眠后重试,不会终止进程。

用法:
    uv run main.py            # 常驻监控(默认)
    uv run main.py --once     # 只查询一轮后退出(调试/cron 场景)
    uv run main.py --dry-run  # 只查询打印,不发邮件不改状态(隐含 --once)
    uv run main.py --reset    # 清空已见记录(隐含 --once)

配置: 同目录 config.yaml,参考 config_example.yaml。
"""
import json
import logging
import os
import re
import signal
import smtplib
import sys
import time
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, quote, urlparse

import requests
import yaml

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
STATE_PATH = os.path.join(BASE_DIR, "state.json")


class ICWClient:
    """登录 icw 并查询学生收入流水。"""

    SSO_LOGIN = "https://sso.buaa.edu.cn/login"
    ICW_CHECK = "http://icw.buaa.edu.cn/checkbuaa.aspx"
    ICW_JUMP = "https://icw.buaa.edu.cn/jump.aspx?sysid=wscx"
    API_LOGIN = "https://chaxun.icw.buaa.edu.cn/api-zhcx/login"
    API_SRCX = "https://chaxun.icw.buaa.edu.cn/api-zhcx/getStudentSRCX"

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = requests.session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/155.0.0.0 Safari/537.36",
        })
        self.token = None

    def _sso_login(self) -> None:
        """SSO 登录并跟随重定向建立 icw 会话。"""
        resp = self.session.get(self.SSO_LOGIN, params={"service": self.ICW_CHECK})
        m = re.search(r'name="execution"\s+value="([^"]+)"', resp.text)
        if not m:
            raise RuntimeError("SSO 登录页中未找到 execution 字段")

        resp = self.session.post(
            self.SSO_LOGIN,
            data={
                "username": self.username,
                "password": self.password,
                "submit": quote("登录"),
                "type": "username_password",
                "execution": m.group(1),
                "_eventId": "submit",
            },
            headers={"Referer": self.SSO_LOGIN,
                     "Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        )
        if resp.status_code != 302:
            raise RuntimeError(f"SSO 登录失败 (status={resp.status_code}),请检查账号密码")

        resp = self.session.get(resp.headers["Location"], allow_redirects=True)
        if "Newindex.aspx" not in resp.url:
            raise RuntimeError(f"icw 会话建立异常,最终 URL: {resp.url}")

    def _api_login(self) -> None:
        """通过 jump.aspx 换取一次性 Cypt 票据,再调查询系统 API 登录拿 JWT。"""
        resp = self.session.get(self.ICW_JUMP, allow_redirects=False)
        location = resp.headers.get("Location", "")
        if resp.status_code != 302 or "Cypt" not in location:
            raise RuntimeError(f"jump.aspx 跳转异常 (status={resp.status_code})")

        qs = parse_qs(urlparse(location).query)
        resp = self.session.post(
            self.API_LOGIN,
            data={
                "username": qs["Userid"][0],
                "password": qs["Cypt"][0],
                "yzm": "yzm",
                "stamp": qs["Stamp"][0],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": location},
        )
        payload = resp.json()
        if payload.get("code") != 200:
            raise RuntimeError(f"查询系统 API 登录失败: {payload.get('message')}")
        self.token = payload["data"]["token"]

    def login(self) -> None:
        self._sso_login()
        self._api_login()
        logging.info("登录成功,token 有效期至 %s",
                     datetime.now().strftime("%Y-%m-%d %H:%M"))

    def fetch_incomes(self, page_size: int = 100) -> list:
        """分页拉取全部学生收入流水,按录入日期倒序返回。"""
        rows, page = [], 1
        while True:
            resp = self.session.post(
                self.API_SRCX,
                json={"userId": self.username,
                      "currentPage": page, "limit": page_size},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            payload = resp.json()
            if payload.get("code") != 200:
                raise RuntimeError(f"收入查询失败: {payload.get('message')}")

            data = payload["data"]
            batch = data.get("data", [])
            rows.extend(batch)
            if len(rows) >= data.get("count", 0) or not batch:
                break
            page += 1

        logging.info("共获取 %d 条收入记录", len(rows))
        return rows


class Notifier:
    """邮件通知。"""

    def __init__(self, cfg: dict):
        self.host = cfg["smtp_host"]
        self.port = int(cfg.get("smtp_port", 465))
        self.user = cfg["username"]
        self.password = cfg["password"]
        self.from_addr = cfg.get("from", self.user)
        self.to_addrs = cfg["to"] if isinstance(cfg["to"], list) else [cfg["to"]]
        self.use_ssl = bool(cfg.get("use_ssl", True))
        self.use_tls = bool(cfg.get("use_starttls", False))

    def send(self, subject: str, html: str, text: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        if self.use_ssl:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=30)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=30)
        try:
            if self.use_tls:
                server.starttls()
            if self.user and self.password:
                server.login(self.user, self.password)
            server.sendmail(self.from_addr, self.to_addrs, msg.as_string())
        finally:
            server.quit()
        logging.info("邮件已发送至 %s", ", ".join(self.to_addrs))


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"seen": [], "first_run": True}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def format_records(rows: list) -> tuple:
    """把新到账记录渲染成邮件正文表格,返回 (html, text)。

    表格列: 发放项目、摘要、金额、税率、税额、实发金额、录入日期。
    """
    def fmt(row, v):
        value = row.get(v)
        return "--" if value in (None, "", "--") else str(value)

    def fmt_tax(row):
        """税率: 优先取字符串百分比 slbfb,否则用数值 sl 换算。"""
        if row.get("slbfb"):
            return str(row["slbfb"])
        sl = row.get("sl")
        if sl in (None, ""):
            return "--"
        return f"{float(sl) * 100:g}%"

    def fmt_num(v):
        return "--" if v in (None, "") else f"{float(v):,.2f}"

    lines, trs = [], []
    total = 0.0
    for row in rows:
        total += float(row.get("sfje") or row.get("je") or 0)
        lines.append(" | ".join([
            fmt(row, "ffxmmc"), fmt(row, "zy"),
            fmt_num(row.get("je")), fmt_tax(row),
            fmt_num(row.get("se")), fmt_num(row.get("sfje")),
            fmt(row, "lrrq")]))
        trs.append(
            "<tr>"
            f"<td>{fmt(row, 'ffxmmc')}</td>"
            f"<td>{fmt(row, 'zy')}</td>"
            f"<td style='text-align:right'>{fmt_num(row.get('je'))}</td>"
            f"<td style='text-align:right'>{fmt_tax(row)}</td>"
            f"<td style='text-align:right'>{fmt_num(row.get('se'))}</td>"
            f"<td style='text-align:right'>{fmt_num(row.get('sfje'))}</td>"
            f"<td>{fmt(row, 'lrrq')}</td>"
            "</tr>")

    head = ("共 %d 笔,实发合计 %s 元。" % (len(rows), f"{total:,.2f}"))
    html = f"""
<html><body style="font-family:-apple-system,'PingFang SC',sans-serif;color:#333">
<p style="font-size:15px">{head}</p>
<table border="1" cellpadding="6" cellspacing="0"
       style="border-collapse:collapse;border-color:#ddd;font-size:13px">
<tr style="background:#f5f5f5">
  <th>发放项目</th><th>摘要</th><th>金额(元)</th><th>税率</th>
  <th>税额(元)</th><th>实发金额(元)</th><th>录入日期</th>
</tr>
{''.join(trs)}
</table>
<p style="color:#999;font-size:12px">来自 Auto-BUAA-icw 自动监控 · {datetime.now():%Y-%m-%d %H:%M}</p>
</body></html>"""
    text = head + "\n\n" + "\n".join(lines)
    return html, text


def load_config() -> dict | None:
    if not os.path.exists(CONFIG_PATH):
        logging.error("未找到 config.yaml,请参考 config_example.yaml 创建。")
        return None
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_file_logging() -> None:
    """常驻模式下把日志同时写入 logs/check.log(1MB x 5 轮转)。"""
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(log_dir, "check.log"),
                                  maxBytes=1024 * 1024, backupCount=5,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(handler)


def check_once(config: dict, dry_run: bool = False, reset: bool = False) -> int:
    """执行一轮:登录 -> 拉流水 -> 对比状态 -> 通知 -> 持久化。"""
    student = config["student"]
    client = ICWClient(student["username"], student["password"])
    client.login()
    rows = client.fetch_incomes(int(config.get("query", {}).get("limit", 100)))

    if dry_run:
        for row in rows:
            logging.info("%s | %s | %s | 金额 %s | 实发 %s | %s",
                         row.get("lrrq"), row.get("ffxmmc"), row.get("zy"),
                         row.get("je"), row.get("sfje"), row.get("nian"))
        logging.info("dry-run: 不对比状态、不发邮件。")
        return 0

    state = load_state()
    if reset:
        state = {"seen": [], "first_run": True}
        logging.info("状态已重置。")

    seen = set(state["seen"])
    fresh = [row for row in rows if row.get("lsh") not in seen]
    is_first_run = state.get("first_run", not seen)

    # 计算本轮需要通知的记录:平时只通知新增;
    # 初次部署(本地无持久化状态)默认把全部存量流水发一封基线邮件,
    # 可通过 notify_on_first_run: false 关闭(则首次只建基线、不通知)。
    notify_rows = []
    if is_first_run:
        if fresh and config.get("notify_on_first_run", True):
            notify_rows = fresh
    else:
        notify_rows = fresh

    # 先发邮件、成功后才持久化新见流水号;发送失败时保持旧状态,下一轮自动重试
    if notify_rows:
        html, text = format_records(notify_rows)
        if is_first_run:
            subject = f"窝囊费监控已启动,已录入 {len(notify_rows)} 笔"
        else:
            subject = f"你有新的{len(notify_rows)}笔窝囊费已录入系统"
        try:
            Notifier(config["mail"]).send(subject, html, text)
        except Exception as exc:
            logging.error("邮件发送失败(状态未更新,下一轮将重试): %s", exc)
            return 2
        if is_first_run:
            logging.info("初次部署,已发送基线邮件(共 %d 笔)。", len(notify_rows))
        else:
            logging.info("发现 %d 笔新收入并已通知。", len(notify_rows))
    elif is_first_run:
        logging.info("初次部署:已记录 %d 条存量流水作为基线,不发送通知。", len(fresh))
    else:
        logging.info("暂无新收入。")

    state["seen"] = [row["lsh"] for row in rows if row.get("lsh")]
    state["first_run"] = False
    state["last_check"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    return 0


def run_forever(config: dict) -> None:
    """常驻主循环:每 check_interval 秒检查一轮,异常记日志后短休眠重试。"""
    interval = int(config.get("check_interval", 1800))
    logging.info("常驻监控启动,检查间隔 %d 秒 (Ctrl-C 退出)", interval)
    while True:
        try:
            check_once(config)
        except KeyboardInterrupt:
            raise
        except Exception:
            logging.exception("本轮检查失败,60 秒后重试")
            time.sleep(60)
            continue
        time.sleep(interval)


def main() -> int:
    args = sys.argv[1:]
    # --dry-run / --reset 都是单轮操作;默认常驻,--once 显式单轮
    once = ("--once" in args or "--dry-run" in args or "--reset" in args)
    dry_run = "--dry-run" in args
    reset = "--reset" in args

    config = load_config()
    if config is None:
        return 1

    if once:
        return check_once(config, dry_run=dry_run, reset=reset)

    setup_file_logging()

    def _term_handler(signum, frame):
        # SIGTERM(如 systemd/procd 停止服务)转为 KeyboardInterrupt,优雅退出
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term_handler)
    try:
        run_forever(config)
    except KeyboardInterrupt:
        logging.info("收到中断信号,程序退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
