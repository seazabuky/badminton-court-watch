#!/usr/bin/env python3
"""
notify_text.py
--------------
แปลง latest.json เป็นข้อความสั้นสำหรับส่งเข้ามือถือ
- กรองเฉพาะเวลาเริ่มเล่นในช่วงที่สนใจ (ค่าเริ่มต้น 15:00-19:00)
- แยกพื้นยาง / พื้นมาสติก ชัดเจน
- แสดงเป็นรอบเล่นยาว 2 ชม. ที่ไม่ซ้อนกัน ไล่จากเวลาเริ่มเร็วสุด
- เทียบกับ latest.json ของ commit ก่อนหน้า (ถ้าเป็นสัปดาห์เป้าหมายเดียวกัน)

ตัวอย่าง
    python notify_text.py
    python notify_text.py --from 15:00 --to 19:00
    python notify_text.py --prev none        # ไม่เทียบรอบก่อน
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TH_MON = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
          "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
SHORT_DOW = {"จันทร์": "จ.", "อังคาร": "อ.", "พุธ": "พ.", "พฤหัส": "พฤ.",
             "ศุกร์": "ศ.", "เสาร์": "ส.", "อาทิตย์": "อา."}
BKK = timezone(timedelta(hours=7))


def mins(t: str) -> int:
    h, m = t.split(":")[:2]
    return int(h) * 60 + int(m)


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def th_date(iso: str) -> str:
    y, mo, d = (int(x) for x in iso.split("-"))
    return f"{d} {TH_MON[mo - 1]}"


def ranges(starts: list[int], step: int = 30) -> str:
    """[900, 930, 960, 1140] -> '15:00–16:00, 19:00'"""
    if not starts:
        return ""
    starts = sorted(set(starts))
    out, a, b = [], starts[0], starts[0]
    for s in starts[1:]:
        if s - b == step:
            b = s
        else:
            out.append(hhmm(a) if a == b else f"{hhmm(a)}–{hhmm(b)}")
            a = b = s
    out.append(hhmm(a) if a == b else f"{hhmm(a)}–{hhmm(b)}")
    return ", ".join(out)


def slots(starts: list[int], dur: int) -> str:
    """เวลาเริ่มที่เป็นไปได้ -> รอบเล่นยาว dur นาทีที่ไม่ซ้อนกัน ไล่จากเร็วสุด
    [900, 930, 960] (dur 120) -> '15:00–17:00'
    [900 ... 1140] -> '15:00–17:00, 17:00–19:00, 19:00–21:00'"""
    out, free_from = [], -1
    for s in sorted(set(starts)):
        if s >= free_from:
            out.append(f"{hhmm(s)}–{hhmm(s + dur)}")
            free_from = s + dur
    return ", ".join(out)


def pair_key(label: str) -> int:
    return int(label.split("+")[0])


def window_view(data: dict, lo: int, hi: int) -> dict:
    """{date: {floor_name: {pair: [start_mins...]}}} เฉพาะเวลาเริ่มใน [lo, hi]"""
    view: dict = {}
    for day in data.get("days", []):
        floors: dict = {}
        for g in day.get("groups", []):
            pairs = floors.setdefault(g["name"], {})
            for p in g["pairs"]:
                st = [mins(s) for b in p["blocks"] for s in b.get("starts", [])
                      if lo <= mins(s) <= hi]
                pairs[p["label"]] = sorted(set(st))
        view[day["date"]] = floors
    return view


def load_prev(path: str, file: Path) -> dict | None:
    if path == "none":
        return None
    if path != "auto":
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    try:
        # หา commit ก่อนหน้าที่แก้ latest.json (ข้าม commit ปัจจุบัน)
        revs = subprocess.run(["git", "log", "--format=%H", "-n", "2", "--", file.name],
                              cwd=file.parent, capture_output=True, text=True, check=True
                              ).stdout.split()
        if len(revs) < 2:
            return None
        raw = subprocess.run(["git", "show", f"{revs[1]}:{file.name}"], cwd=file.parent,
                             capture_output=True, text=True, check=True).stdout
        return json.loads(raw)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="สรุป latest.json เป็นข้อความ")
    ap.add_argument("--file", type=Path, default=Path(__file__).with_name("latest.json"))
    ap.add_argument("--from", dest="lo", default="15:00", help="เริ่มเล่นเร็วสุด")
    ap.add_argument("--to", dest="hi", default="19:00", help="เริ่มเล่นช้าสุด")
    ap.add_argument("--prev", default="auto", help="auto | none | path ของ latest.json รอบก่อน")
    args = ap.parse_args()

    try:
        data = json.loads(args.file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"อ่าน {args.file} ไม่ได้: {exc}")

    lo, hi = mins(args.lo), mins(args.hi)
    dur = int(data.get("duration", 120))
    days = data["days"]
    view = window_view(data, lo, hi)
    lines: list[str] = []

    first, last = days[0], days[-1]
    lines.append(f"🏸 คอร์ทแบดติดกันว่าง {dur // 60} ชม. · สนามพิริยะ")
    lines.append(f"{SHORT_DOW.get(first['dow'], first['dow'])} {th_date(first['date'])} – "
                 f"{SHORT_DOW.get(last['dow'], last['dow'])} {th_date(last['date'])} · "
                 f"เริ่มเล่น {args.lo}–{args.hi}")
    ud, ut = data["updated"].split(" ")
    lines.append(f"ข้อมูลอัปเดต {th_date(ud)} {ut} น.")

    today = datetime.now(BKK).strftime("%Y-%m-%d")
    if ud != today:
        lines.append("")
        lines.append(f"⚠️ ข้อมูลยังไม่อัปเดตวันนี้ (ล่าสุด {th_date(ud)}) "
                     "workflow บน GitHub อาจล่าช้าหรือล้มเหลว")

    # ── เทียบกับรอบก่อน ──
    prev = load_prev(args.prev, args.file)
    if prev and prev.get("target") == data.get("target") and prev.get("updated") != data.get("updated"):
        pview = window_view(prev, lo, hi)
        diffs = []
        dow_of = {d["date"]: d["dow"] for d in days}
        for d in days:
            if not d.get("ok"):
                continue  # วันนี้ดึงไม่สำเร็จ ห้ามสรุปว่าหายไป
            for floor, pairs in view.get(d["date"], {}).items():
                before_floor = pview.get(d["date"], {}).get(floor, {})
                for pair in sorted(set(pairs) | set(before_floor), key=pair_key):
                    now_s, old_s = set(pairs.get(pair, [])), set(before_floor.get(pair, []))
                    tag = f"{SHORT_DOW.get(dow_of[d['date']], '')} {th_date(d['date'])} {floor} {pair}"
                    if old_s - now_s:
                        diffs.append(f"− หายไป {tag} {slots(list(old_s - now_s), dur)}")
                    if now_s - old_s:
                        diffs.append(f"+ ว่างเพิ่ม {tag} {slots(list(now_s - old_s), dur)}")
        pu_d, pu_t = prev["updated"].split(" ")
        lines.append("")
        if diffs:
            lines.append(f"เปลี่ยนจากรอบก่อน ({th_date(pu_d)} {pu_t}):")
            lines.extend(diffs)
        else:
            lines.append(f"ไม่มีอะไรเปลี่ยนจากรอบก่อน ({th_date(pu_d)} {pu_t})")

    # ── รายวัน แยกพื้น ──
    for d in days:
        lines.append("")
        lines.append(f"━━ {d['dow']} {th_date(d['date'])} ━━")
        if not d.get("ok"):
            lines.append(f"ดึงข้อมูลไม่สำเร็จ: {d.get('error') or '-'}")
            continue
        if d.get("closed"):
            lines.append("สนามปิด")
            continue
        for g in d.get("groups", []):
            note = g["full"][len(g["name"]):].strip()
            lines.append(f"{g['name']}{' ' + note if note else ''}")
            pairs = view[d["date"]].get(g["name"], {})
            found = [(p, s) for p, s in sorted(pairs.items(), key=lambda kv: pair_key(kv[0])) if s]
            if not found:
                lines.append("- ไม่มีคู่ว่างในช่วงนี้")
            for p, s in found:
                lines.append(f"- คอร์ท {p}  {slots(s, dur)}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
