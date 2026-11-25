#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监督员：检查目标 workflow 当天是否在指定时段成功运行；没有则触发补跑；记录到状态表。
由外部 cron-job.org 通过 workflow_dispatch 触发，slot 参数决定检查哪个时段。
查岗/补跑均使用 GitHub 自带的 GITHUB_TOKEN，不依赖个人 PAT。
"""
import os, sys, json, urllib.request, urllib.error, pathlib
from datetime import datetime, timedelta, timezone

TOKEN = os.environ.get("GH_TOKEN", "")
REPO  = os.environ.get("REPO", "")      # owner/name，如 15014352135/xuanjige-v7
SLOT  = os.environ.get("SLOT", "")      # main / morning / afternoon

# 每个时段对应的目标 workflow、展示名、计划时间、允许的成功时间窗（北京时间小时）
CFG = {
    "main":      {"target": "00-daily-analysis.yml", "label": "每日股票分析", "plan": "16:15", "win": (16, 24), "inputs": {"mode": "full", "force_run": "false"}},
    "morning":   {"target": "v7-morning.yml",        "label": "v7-盘前选股",  "plan": "08:35", "win": (6, 13),  "inputs": None},
    "afternoon": {"target": "v7-afternoon.yml",      "label": "v7-盘中选股",  "plan": "15:45", "win": (12, 24), "inputs": None},
}

if SLOT not in CFG:
    print(f"::error::未知 SLOT={SLOT}，应为 {list(CFG)}")
    sys.exit(2)
cfg = CFG[SLOT]

def api(method, path, body=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {TOKEN}",
        "User-Agent": "supervisor-bot",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode(errors="replace")
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")

bj = timezone(timedelta(hours=8))
today = datetime.now(bj).strftime("%Y-%m-%d")
lo, hi = cfg["win"]

# 1) 查目标 workflow 今天是否在时间窗内成功跑过
status, run_url, note = None, "", ""
st, data = api("GET", f"/repos/{REPO}/actions/workflows/{cfg['target']}/runs?per_page=100")
found = None
if st == 200 and isinstance(data, dict):
    for run in data.get("workflow_runs", []):
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        try:
            ct = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).astimezone(bj)
        except Exception:
            continue
        if ct.strftime("%Y-%m-%d") == today and lo <= ct.hour < hi:
            found = run
            break

if found:
    status, run_url, note = "ok", found.get("html_url", ""), "当天已按时完成"
else:
    body = {"ref": "main"}
    if cfg["inputs"]:
        body["inputs"] = cfg["inputs"]
    st2, resp = api("POST", f"/repos/{REPO}/actions/workflows/{cfg['target']}/dispatches", body)
    if st2 in (200, 201, 204):
        status, note = "rerun", "当天未按时完成，已触发补跑"
    else:
        status, note = "fail", f"补跑触发失败 HTTP {st2}: {str(resp)[:200]}"

# 2) 写状态记录（同一天同一时段幂等覆盖）
rec_dir = pathlib.Path("data/supervision")
rec_dir.mkdir(parents=True, exist_ok=True)
rec_file = rec_dir / "STATUS.jsonl"
recs = []
if rec_file.exists():
    for line in rec_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
rec = {
    "date": today, "slot": SLOT, "label": cfg["label"], "plan": cfg["plan"],
    "status": status, "note": note, "run_url": run_url,
    "checked_at": datetime.now(bj).strftime("%Y-%m-%d %H:%M:%S"),
}
recs = [r for r in recs if not (r.get("date") == today and r.get("slot") == SLOT)]
recs.append(rec)
rec_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")

# 3) 生成可读状态表（用户打开就能看到打勾）
icon = {"ok": "✅ 按时完成", "rerun": "⚠️ 未按时→已补跑", "fail": "❌ 补跑失败"}
lines = [
    "# 每日运行监督表",
    "",
    "> 由监督员自动维护，请勿手动编辑。时间为北京时间(UTC+8)。",
    "",
    "| 日期 | 时段 | 计划时间 | 结果 | 检查时间 | 运行链接 |",
    "|---|---|---|---|---|---|",
]
for r in sorted(recs, key=lambda x: (x.get("date", ""), x.get("slot", "")), reverse=True):
    link = f"[查看]({r['run_url']})" if r.get("run_url") else "—"
    lines.append(
        f"| {r.get('date')} | {r.get('label')} | {r.get('plan')} | "
        f"{icon.get(r.get('status'), r.get('status'))} | {r.get('checked_at')} | {link} |"
    )
pathlib.Path("SUPERVISION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

print(f"[{today}] {cfg['label']} (计划 {cfg['plan']}): {icon[status]} — {note}")
