#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2Libra 每日自动签到（云端版 / GitHub Actions）

与本地版是同一份脚本，凭据来源做了兼容：
    优先读环境变量 TWOLIBRA_TOKEN  -> 云端用 GitHub Secrets
    其次读同目录 config.json       -> 本地手动运行用
因此同一个文件在本地和云端都能跑。

接口（均为站内官方接口）：
    POST /api/sign                            -> 执行签到（独立接口，幂等）
    GET  /api/users/info?fields=info,exp,coins-> 账户信息 / 等级经验 / 金币 / 六维属性
    GET  /api/sign/stats                      -> 连续签到 / 历史最长
    GET  /api/coins/today-transaction         -> 今日金币流水（含签到骰子明细）

用法：
    python checkin.py            # 签到 + 输出账户摘要
    python checkin.py --check    # 只查询状态，不触发签到
    python checkin.py --notify   # 签到后把结果推送到手机
    python checkin.py --debug    # 额外打印各接口原始 JSON
    python checkin.py --plan     # 只打印本次「随机签到计划」，不等待也不签到
    python checkin.py --no-random# 忽略随机延迟，立即签到
    python checkin.py --delay-seconds 5   # 手动指定等待秒数（调试用）

签到时刻调度（配置文件 schedule.json，与本脚本同目录）：
    mode = "sleep"   作业内等待：cron 触发一次，脚本随机等 60~180 分钟再签到
                     成本高（等待时间真实计入 Actions 分钟数）
    mode = "slot"    时段抽签：cron 每 10 分钟触发一次，每天抽一个目标时刻，
                     只有到点的那次真正签到，其余各次秒退（零接口请求）
                     成本低约 30 倍，但每次触发仍会跑一个很短的作业

    base_time            基准时间（北京时间），slot 模式用它推算当日目标时刻
    delay_min_minutes    随机区间下限（默认 60）
    delay_max_minutes    随机区间上限（默认 180）
    slot_min_minutes     slot 模式的 cron 触发间隔（默认 10，需与 workflow 一致）
    jitter_seed          留空即自动：sleep 模式每次真随机，slot 模式用 token 派生种子
    环境变量 TWOLIBRA_DELAY_MIN / TWOLIBRA_DELAY_MAX / TWOLIBRA_JITTER_SEED 可覆盖上述配置

    手动触发（网页/手机点 Run workflow，或本地手跑）不参与抽签，一律直接签到。

手机推送（配置任一环境变量即可，见 README）：
    PUSH_URL        企业微信群机器人 webhook
    PUSHPLUS_TOKEN  PushPlus（微信推送）
    GITHUB_STEP_SUMMARY  GitHub Actions 自动注入，会把结果写进任务摘要

退出码：0 成功 / 1 运行异常 / 2 凭据失效(需重新获取 token)
"""

import argparse
import datetime
import hashlib
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://2libra.com"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SCHEDULE_PATH = os.path.join(HERE, "schedule.json")
LOG_DIR = os.path.join(HERE, "logs")

# 签到时刻配置（可被同目录 schedule.json 覆盖）
#   mode = sleep  作业内等待：cron 只触发一次，触发后随机等一段时间再签到
#   mode = slot   时段抽签：cron 按固定间隔触发多次，每天抽一个目标时刻，
#                 只有到点的那一次真正签到，其余各次秒退（不产生任何接口请求）
DEFAULT_SCHEDULE = {
    "mode": "sleep",
    "base_time": "08:05",        # 北京时间基准，slot 模式用它算目标时刻
    "delay_min_minutes": 60,     # 随机区间下限（分钟）
    "delay_max_minutes": 180,    # 随机区间上限（分钟）
    "slot_min_minutes": 10,      # slot 模式：cron 触发间隔（分钟），需与 workflow 一致
    "jitter_seed": "",           # 留空 = sleep 模式每次真随机 / slot 模式用 token 派生种子
}
SLEEP_HEARTBEAT_SEC = 300        # 长等待期间每 5 分钟打印一次进度
STATE_FILE = "last-signed.txt"   # 记录「今天已签到」，供 slot 模式各次触发共享
MODE_SLEEP = "sleep"
MODE_SLOT = "slot"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

DEBUG = False

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


class Unauthorized(Exception):
    """凭据失效，需要重新抓取 token。"""


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_log(line):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fname = os.path.join(LOG_DIR, "checkin-%s.log" % datetime.datetime.now().strftime("%Y-%m"))
        with open(fname, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fail(msg, code=1):
    line = "[%s] 失败: %s" % (now_str(), msg)
    print(line)
    write_log(line)
    push_notice("2Libra 签到失败", line, warning=True)
    sys.exit(code)


def load_config():
    """凭据优先级：环境变量 TWOLIBRA_TOKEN > config.json。"""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    token = str(os.environ.get("TWOLIBRA_TOKEN") or cfg.get("access_token") or "").strip()
    if not token or token.startswith("在这里"):
        fail("未找到 access_token。云端：请设置 TWOLIBRA_TOKEN Secret；本地：请填入 config.json。", 1)
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    cfg["access_token"] = token
    cfg["cookie"] = str(os.environ.get("TWOLIBRA_COOKIE") or cfg.get("cookie") or "").strip()
    return cfg


def build_headers(cfg):
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "authorization": "Bearer " + cfg["access_token"],
        "referer": BASE + "/",
        "origin": BASE,
        "user-agent": USER_AGENT,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if cfg.get("cookie"):
        headers["cookie"] = cfg["cookie"]
    return headers


def parse_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {"c": -1, "m": raw[:300], "d": None}


def request(cfg, method, path, body=None, tries=3, timeout=15):
    """返回 (http_status, json_body)。HTTP 4xx 不抛异常，交给上层判断。"""
    url = BASE + path
    headers = build_headers(cfg)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"

    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                out = (resp.status, parse_json(raw))
                if DEBUG:
                    print("[debug] %s %s -> %s %s" % (method, path, out[0], json.dumps(out[1], ensure_ascii=False)[:1500]))
                return out
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            out = (e.code, parse_json(raw))
            if DEBUG:
                print("[debug] %s %s -> %s %s" % (method, path, out[0], json.dumps(out[1], ensure_ascii=False)[:1500]))
            return out
        except Exception as e:  # 网络抖动时重试
            last_err = e
            if DEBUG:
                print("[debug] %s %s -> 异常(第%s次): %s" % (method, path, attempt, e))
            if attempt < tries:
                time.sleep(2 * attempt)
    return 0, {"c": -1, "m": "网络错误: %s" % last_err, "d": None}


def is_unauthorized(resp):
    if not isinstance(resp, dict):
        return False
    if resp.get("c") in (401, 403):
        return True
    msg = str(resp.get("m", "")).lower()
    return "unauthorized" in msg or "未登录" in msg


def dig(resp):
    return resp.get("d") if isinstance(resp, dict) else None


def token_expired(cfg):
    """本地解析 JWT 的 exp，提前给出过期预警（不校验签名）。"""
    import base64
    try:
        payload = cfg["access_token"].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if exp:
            return datetime.datetime.fromtimestamp(exp)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# 随机签到时间
# --------------------------------------------------------------------------- #
def _as_int(value, default):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def load_schedule():
    """读取 schedule.json；环境变量优先级高于文件。"""
    sched = dict(DEFAULT_SCHEDULE)
    if os.path.exists(SCHEDULE_PATH):
        try:
            with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in DEFAULT_SCHEDULE:
                    if key in data:
                        sched[key] = data[key]
        except Exception as e:
            print("[计划] schedule.json 解析失败，改用默认值：%s" % e)

    for env_key, cfg_key in (("TWOLIBRA_DELAY_MIN", "delay_min_minutes"),
                             ("TWOLIBRA_DELAY_MAX", "delay_max_minutes"),
                             ("TWOLIBRA_JITTER_SEED", "jitter_seed")):
        raw = os.environ.get(env_key)
        if raw not in (None, ""):
            sched[cfg_key] = raw.strip()

    sched["delay_min_minutes"] = max(0, _as_int(sched["delay_min_minutes"], 60))
    sched["delay_max_minutes"] = max(0, _as_int(sched["delay_max_minutes"], 180))
    if sched["delay_max_minutes"] < sched["delay_min_minutes"]:
        sched["delay_max_minutes"] = sched["delay_min_minutes"]
    sched["slot_min_minutes"] = max(1, _as_int(sched.get("slot_min_minutes"), 10))
    sched["base_time"] = str(sched.get("base_time") or "").strip()
    sched["jitter_seed"] = str(sched.get("jitter_seed") or "").strip()

    mode = str(sched.get("mode") or MODE_SLEEP).strip().lower()
    if mode not in (MODE_SLEEP, MODE_SLOT):
        print("[计划] 未知 mode=%r，已回退为 %s。" % (mode, MODE_SLEEP))
        mode = MODE_SLEEP
    sched["mode"] = mode
    return sched


def beijing_now():
    """统一用北京时间（UTC+8）判定，避免 runner 的 UTC 时区造成跨日错乱。"""
    utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return utc + datetime.timedelta(hours=8)


def parse_hhmm(text):
    try:
        hh, mm = str(text).strip().split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    except Exception:
        pass
    return None


def schedule_salt(cfg):
    """slot 模式没配 jitter_seed 时用 token 派生种子：对外不可预测，对内稳定。"""
    return hashlib.sha256(("2libra|" + cfg.get("access_token", "")).encode("utf-8")).hexdigest()[:16]


def daily_target(sched, salt=""):
    """今天的随机目标时刻（按北京时间），同一天内稳定不变；base_time 非法时返回 None。"""
    base = parse_hhmm(sched.get("base_time"))
    if not base:
        return None
    lo, hi = sched["delay_min_minutes"] * 60, sched["delay_max_minutes"] * 60
    now = beijing_now()
    if hi <= lo:
        offset = lo
    else:
        seed = sched.get("jitter_seed") or salt or "2libra"
        digest = hashlib.sha256(("%s|%s" % (now.date().isoformat(), seed)).encode("utf-8")).hexdigest()
        offset = lo + int(digest, 16) % (hi - lo + 1)
    anchor = now.replace(hour=base[0], minute=base[1], second=0, microsecond=0)
    return anchor + datetime.timedelta(seconds=offset)


def read_last_signed():
    """返回上次成功签到的北京日期字符串（YYYY-MM-DD），无记录返回空串。"""
    try:
        with open(os.path.join(LOG_DIR, STATE_FILE), "r", encoding="utf-8") as f:
            return f.read().strip().split(" ")[0]
    except Exception:
        return ""


def write_last_signed():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, STATE_FILE), "w", encoding="utf-8") as f:
            f.write("%s 签于 %s（北京时间）\n" % (beijing_now().date().isoformat(), beijing_now().strftime("%H:%M:%S")))
    except Exception as e:
        print("[状态] 写入签到状态失败（不影响签到结果）：%s" % e)


def fmt_dur(seconds):
    seconds = int(max(0, seconds))
    hours, minutes, secs = seconds // 3600, seconds % 3600 // 60, seconds % 60
    if hours:
        return "%d 小时 %d 分" % (hours, minutes)
    if minutes:
        return "%d 分 %d 秒" % (minutes, secs)
    return "%d 秒" % secs


def _first_int(token):
    """从 cron 字段取首个值，兼容 "5,15,25" / "0-3" / "*/10" 等写法。"""
    token = str(token).split(",")[0]
    for sep in ("-", "/"):
        if sep in token:
            token = token.split(sep)[0]
    return int(token)


def parse_cron_beijing(cron):
    """把 workflow 的 cron（UTC）换算成北京时间 HH:MM，用于一致性校验。"""
    try:
        parts = str(cron).split()
        minute, hour = _first_int(parts[0]), _first_int(parts[1])
        t = (datetime.datetime(2000, 1, 1, hour, minute) + datetime.timedelta(hours=8)).time()
        return "%02d:%02d" % (t.hour, t.minute)
    except Exception:
        return None


def pick_delay_seconds(sched):
    """sleep 模式：随机等多久，返回 (秒数, 说明)。"""
    lo = sched["delay_min_minutes"] * 60
    hi = sched["delay_max_minutes"] * 60
    if hi <= 0:
        return 0, "随机延迟已关闭"
    seed = sched.get("jitter_seed")
    if seed:
        digest = hashlib.sha256(("%s|%s" % (beijing_now().date().isoformat(), seed)).encode("utf-8")).hexdigest()
        return lo + int(digest, 16) % (hi - lo + 1), "按种子 %s 生成（当天多次运行结果一致）" % seed
    return random.randint(lo, hi), "每次真随机"


def print_plan(sched, now_bj, target_at, delay, how, check_only, slotting):
    """打印本次签到时刻计划（同时写入日志）。"""
    if slotting:
        mode_label = "时段抽签 slot（cron 多次触发，到点那次才签到）"
    elif sched["mode"] == MODE_SLOT:
        mode_label = "时段抽签 slot（本次跳过抽签，直接签到）"
    else:
        mode_label = "作业内等待 sleep（触发后随机等待再签到）"

    lines = ["=" * 48, "  签到时刻计划", "=" * 48,
             "当前时间  : 北京 %s（UTC %s）" % (now_bj.strftime("%Y-%m-%d %H:%M:%S"),
                                             (now_bj - datetime.timedelta(hours=8)).strftime("%m-%d %H:%M:%S")),
             "调度模式  : " + mode_label,
             "基准时间  : %s（北京时间）" % (sched.get("base_time") or "-")]
    cron = (os.environ.get("SCHEDULED_CRON") or "").strip()
    if cron:
        cron_bj = parse_cron_beijing(cron)
        lines.append("workflow  : cron [%s] -> 北京 %s" % (cron, cron_bj))
        if cron_bj and sched.get("base_time") and cron_bj != sched["base_time"]:
            lines.append("提示      : base_time 与 cron 不一致，实际触发以 cron（北京 %s）为准" % cron_bj)
    lines.append("随机区间  : %s ~ %s 分钟（%s）" % (sched["delay_min_minutes"], sched["delay_max_minutes"], how))
    if slotting:
        lines.append("今日目标  : 北京 %s（当天恒定，各次触发一致）" % target_at.strftime("%Y-%m-%d %H:%M:%S"))
        lines.append("本次动作  : " + ("已到点 -> 执行签到" if now_bj >= target_at else "未到点 -> 直接退出，不请求接口"))
    else:
        lines.append("本次延迟  : " + fmt_dur(delay))
        lines.append("计划签到  : 北京 " + target_at.strftime("%Y-%m-%d %H:%M:%S"))
    if check_only:
        lines.append("说明      : --check 只查询，不签到")
    lines.append("=" * 48)
    text = "\n".join(lines)
    print(text)
    write_log(text.replace("\n", " | "))


def sleep_until(target_at):
    """睡到目标时刻，期间定期打印进度，便于在 Actions 日志里确认任务仍在运行。"""
    while True:
        remain = (target_at - beijing_now()).total_seconds()
        if remain <= 0:
            return
        time.sleep(min(remain, SLEEP_HEARTBEAT_SEC))
        remain = (target_at - beijing_now()).total_seconds()
        if remain > 0:
            print("[等待] 距计划签到还有 %s（目标 %s）" % (fmt_dur(remain), target_at.strftime("%H:%M:%S")))


# --------------------------------------------------------------------------- #
# 业务
# --------------------------------------------------------------------------- #
def do_sign(cfg):
    code, resp = request(cfg, "POST", "/api/sign")
    if is_unauthorized(resp):
        raise Unauthorized()
    if code == 0 or (code >= 400 and not is_unauthorized(resp)):
        return False, resp
    return True, resp


def get_account(cfg):
    _, resp = request(cfg, "GET", "/api/users/info?fields=info%2Cexp%2Ccoins")
    if is_unauthorized(resp):
        raise Unauthorized()
    return dig(resp) or {}


def get_stats(cfg):
    """连签统计接口偶发返回空，容错处理。"""
    code, resp = request(cfg, "GET", "/api/sign/stats", tries=2)
    d = dig(resp)
    return d if isinstance(d, dict) else {}


def get_today_checkin_txn(cfg):
    """从今日金币流水中取出签到那一条。"""
    _, resp = request(cfg, "GET", "/api/coins/today-transaction")
    d = dig(resp)
    if not isinstance(d, list):
        return {}
    for item in d:
        if isinstance(item, dict) and item.get("reason") == "checkin":
            return item
    return {}


OUTCOME_CN = {
    "great_success": "大成功（奖励翻倍）",
    "success": "检定成功",
    "failure": "检定失败（按勤勉值折算）",
    "great_failure": "大失败（仅 1 金币）",
}

ATTR_CN = {
    "experience": "阅历",
    "charm": "魅力",
    "luck": "幸运",
    "diligence": "勤勉",
    "craft": "技艺",
    "karma": "财富",
}


# --------------------------------------------------------------------------- #
# 结果整理（本地文本 / 手机 markdown 共用同一份数据）
# --------------------------------------------------------------------------- #
def collect(sign_ok, sign_resp, acct, stats, txn, check_only):
    d = dig(sign_resp) or {}
    v = {
        "time": now_str(),
        "check_only": check_only,
        "sign_ok": sign_ok,
        "account": None,
        "level": None,
        "coins": acct.get("coins"),
        "sign_state": None,
        "check": None,
        "reward": None,
        "streak": None,
        "max_streak": None,
        "attrs": [],
        "exp_left": None,
    }

    name = acct.get("username") or acct.get("display_name")
    if name:
        num = acct.get("user_number")
        v["account"] = "%s%s" % (name, "（%s 号）" % num if num else "")

    exp = acct.get("exp") or {}
    if exp:
        v["level"] = "%s 级  %s / %s（还差 %s 升级）" % (
            exp.get("level", "-"), exp.get("currentExp", "-"),
            exp.get("nextLevelExp", "-"), exp.get("expToNext", "-"))

    if check_only:
        v["sign_state"] = "未触发（--check 模式）"
    elif sign_ok:
        if d.get("alreadySigned"):
            v["sign_state"] = "今天已经签到过了"
        else:
            gained = d.get("coins")
            v["sign_state"] = "签到成功" + ("，本次获得 %s 金币" % gained if gained else "")
    else:
        v["sign_state"] = "签到失败 -> %s" % (sign_resp.get("m") if isinstance(sign_resp, dict) else sign_resp)

    cd = d.get("checkData") or txn.get("check_data") or {}
    if isinstance(cd, dict) and cd and cd.get("roll") is not None:
        reward = cd.get("reward") or {}
        v["check"] = "d20 掷 %s + %s %s = %s，对难度 %s -> %s" % (
            cd.get("roll"), ATTR_CN.get(cd.get("attr"), cd.get("attr") or "属性"),
            cd.get("attrValue", "?"), cd.get("total", "-"),
            cd.get("difficulty"), OUTCOME_CN.get(cd.get("outcome"), cd.get("outcome") or "-"))
        if reward.get("baseCoins") is not None:
            base, final = reward.get("baseCoins"), reward.get("finalCoins")
            v["reward"] = "%s 金币%s" % (base, "" if base == final else "（最终 %s）" % final)
    elif txn.get("amount") is not None:
        v["reward"] = "%s 金币" % txn["amount"]

    v["streak"] = d.get("streak") or stats.get("streak")
    v["max_streak"] = stats.get("maxStreak")

    ua = acct.get("user_attributes") or {}
    for label, key in (("阅历", "experience_attr"), ("魅力", "charm_attr"), ("幸运", "luck_attr"),
                       ("勤勉", "diligence_attr"), ("技艺", "craft_attr"), ("财富", "karma_attr")):
        if ua.get(key) not in (None, ""):
            v["attrs"].append("%s %s" % (label, ua[key]))

    exp_at = token_expired_last
    if exp_at:
        v["exp_left"] = "%s（剩 %s 天）" % (exp_at.strftime("%Y-%m-%d %H:%M"),
                                          (exp_at - datetime.datetime.now()).days)
    return v


def render(v):
    L = ["=" * 48, "  2Libra 每日签到" + ("（仅查询）" if v["check_only"] else ""), "=" * 48,
         "时间      : " + v["time"]]
    if v["account"]:
        L.append("账号      : " + v["account"])
    if v["level"]:
        L.append("等级/经验 : " + v["level"])
    if v["coins"] is not None:
        L.append("金币余额  : %s" % v["coins"])
    L.append("签到      : " + str(v["sign_state"]))
    if v["check"]:
        L.append("勤勉检定  : " + v["check"])
    if v["reward"]:
        L.append("本次奖励  : " + v["reward"])
    if v["streak"] is not None:
        L.append("连续签到  : %s 天%s" % (v["streak"],
                 "（历史最长 %s 天）" % v["max_streak"] if v["max_streak"] is not None else ""))
    if v["attrs"]:
        L.append("属性      : " + " | ".join(v["attrs"]))
    if v["exp_left"]:
        L.append("凭据      : token 有效期至 " + v["exp_left"])
    L.append("=" * 48)
    return "\n".join(L)


def render_markdown(v):
    """企业微信机器人 / PushPlus 友好格式（不支持代码块，用引用行代替）。"""
    ok = v["sign_ok"] and "失败" not in str(v["sign_state"])
    lines = ["**2Libra 每日签到** %s" % ("✅" if ok else "⚠️"), ""]
    lines.append("> 账号：%s" % (v["account"] or "-"))
    lines.append("> 结果：%s" % v["sign_state"])
    if v["reward"]:
        lines.append("> 本次奖励：**%s**" % v["reward"])
    if v["streak"] is not None:
        lines.append("> 连续签到：**%s 天**%s" % (v["streak"],
                     "（历史最长 %s 天）" % v["max_streak"] if v["max_streak"] is not None else ""))
    if v["coins"] is not None:
        lines.append("> 金币余额：**%s**" % v["coins"])
    if v["level"]:
        lines.append("> 等级：%s" % v["level"])
    if v["check"]:
        lines.append("> 勤勉检定：%s" % v["check"])
    if v["attrs"]:
        lines.append("> 属性：%s" % " | ".join(v["attrs"]))
    if v["exp_left"]:
        warn = ""
        try:
            if int(v["exp_left"].split("剩 ")[1].split(" ")[0]) < 7:
                warn = " ⚠️ 请尽快更换 token"
        except Exception:
            pass
        lines.append("> token 剩余：%s%s" % (v["exp_left"], warn))
    lines.append("")
    lines.append("> 时间：%s" % v["time"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 手机推送
# --------------------------------------------------------------------------- #
def http_post_json(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def push_notice(title, content, warning=False):
    """散装文本推送（用于异常提示）。"""
    body = "> <font color=\"%s\">%s</font>\n\n> %s" % (
        "warning" if warning else "info", title, content.replace("\n", "\n> "))
    _do_push(body, title)


def push_result(v):
    _do_push(render_markdown(v), "2Libra 每日签到")


def _do_push(markdown_body, title):
    sent = False

    url = (os.environ.get("PUSH_URL") or "").strip()
    if url:
        try:
            st, body = http_post_json(url, {"msgtype": "markdown", "markdown": {"content": markdown_body}})
            print("[通知] 企业微信 -> HTTP %s %s" % (st, body[:200]))
            sent = True
        except Exception as e:
            print("[通知] 企业微信发送失败: %s" % e)

    pp = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if pp:
        try:
            st, body = http_post_json("https://www.pushplus.plus/send",
                                      {"token": pp, "title": title, "content": markdown_body,
                                       "template": "markdown"})
            print("[通知] PushPlus -> HTTP %s %s" % (st, body[:200]))
            sent = True
        except Exception as e:
            print("[通知] PushPlus 发送失败: %s" % e)

    if not sent and not os.environ.get("GITHUB_STEP_SUMMARY"):
        print("[通知] 未配置 PUSH_URL / PUSHPLUS_TOKEN，跳过推送。")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write("## 2Libra 每日签到\n\n" + markdown_body + "\n")
            print("[通知] 已写入 GitHub 任务摘要。")
        except Exception as e:
            print("[通知] 写入任务摘要失败: %s" % e)


token_expired_last = None


def main():
    global DEBUG, token_expired_last

    ap = argparse.ArgumentParser(description="2Libra 每日自动签到")
    ap.add_argument("--check", action="store_true", help="只查询账户/签到状态，不触发签到")
    ap.add_argument("--notify", action="store_true", help="把结果推送到手机")
    ap.add_argument("--debug", action="store_true", help="打印各接口原始响应")
    ap.add_argument("--plan", action="store_true", help="只打印本次随机签到计划后退出")
    ap.add_argument("--no-random", action="store_true", help="忽略随机延迟，立即签到")
    ap.add_argument("--delay-seconds", type=int, default=None, help="手动指定等待秒数（调试用）")
    args = ap.parse_args()
    DEBUG = args.debug

    cfg = load_config()
    token_expired_last = token_expired(cfg)

    # ---------- 签到时刻调度 ----------
    sched = load_schedule()
    now_bj = beijing_now()
    explicit = args.no_random or args.delay_seconds is not None
    # 只有定时触发才参与抽签；手动点「Run workflow」或本地手跑一律直接签到
    manual = (os.environ.get("GITHUB_EVENT_NAME") or "").strip() != "schedule"
    slotting = (sched["mode"] == MODE_SLOT) and not explicit and not manual

    delay = 0
    if slotting:
        target_at = daily_target(sched, schedule_salt(cfg))
        if target_at is None:
            fail("slot 模式需要合法的 base_time（形如 08:05），当前为 %r。" % sched.get("base_time"), 1)
        how = "按日期+种子固定，当天恒定；种子对外不可预测"
    else:
        if args.delay_seconds is not None:
            delay, how = max(0, args.delay_seconds), "由 --delay-seconds 指定"
        elif args.no_random:
            delay, how = 0, "由 --no-random 指定，不等待"
        elif sched["mode"] == MODE_SLOT:
            delay, how = 0, "手动触发，本次跳过抽签"
        else:
            delay, how = pick_delay_seconds(sched)
        target_at = now_bj + datetime.timedelta(seconds=delay)

    print_plan(sched, now_bj, target_at, delay, how, args.check, slotting)

    if args.plan:
        sys.exit(0)

    if slotting:
        # 时段抽签：未到点就秒退，不产生任何接口请求
        if now_bj < target_at:
            print("[跳过] 未到今日抽中的时刻（北京 %s），本次不签到。" % target_at.strftime("%H:%M:%S"))
            sys.exit(0)
        done = read_last_signed()
        if done == now_bj.date().isoformat():
            print("[跳过] 今天已经签过了（状态文件记录 %s），本次不重复请求接口。" % done)
            sys.exit(0)
        print("[执行] 已到今日抽中的时刻（北京 %s），开始签到……" % target_at.strftime("%H:%M:%S"))
    elif delay > 0:
        # 先验一次凭据，避免白等几小时才发现 token 已失效
        try:
            get_account(cfg)
            print("[计划] 凭据预检通过，token 有效。")
        except Unauthorized:
            fail("access_token 已失效（401 Unauthorized），本次不再等待。请重新登录 2libra.com 抓取新 token 并更新。", 2)
        except Exception as e:
            print("[计划] 预检请求异常（不影响后续签到）：%s" % e)
        print("[等待] 开始等待 %s，预计 %s 执行签到……" % (fmt_dur(delay), target_at.strftime("%H:%M:%S")))
        sleep_until(target_at)

    try:
        sign_ok, sign_resp = (True, {}) if args.check else do_sign(cfg)
        # 三个查询接口互不依赖，并发拉取以缩短整体耗时
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_acct = pool.submit(get_account, cfg)
            f_stats = pool.submit(get_stats, cfg)
            f_txn = pool.submit(get_today_checkin_txn, cfg)
            acct, stats, txn = f_acct.result(), f_stats.result(), f_txn.result()
    except Unauthorized:
        fail("access_token 已失效（401 Unauthorized）。请重新登录 2libra.com 抓取新的 token 并更新。", 2)

    v = collect(sign_ok, sign_resp, acct, stats, txn, args.check)
    text = render(v)
    print(text)
    write_log(text.replace("\n", " | "))

    if sign_ok and not args.check:
        # 记录「今天已签到」：slot 模式靠它让后续各次触发直接跳过，
        # 同时该文件的变化也是 workflow 保活提交的来源。
        write_last_signed()

    auto_push = any(os.environ.get(k) for k in ("PUSH_URL", "PUSHPLUS_TOKEN", "GITHUB_STEP_SUMMARY"))
    if args.notify or auto_push:
        push_result(v)

    sys.exit(0 if (sign_ok or args.check) else 1)


if __name__ == "__main__":
    main()
