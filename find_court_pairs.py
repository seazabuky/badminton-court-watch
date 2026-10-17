#!/usr/bin/env python3
"""
find_court_pairs.py
-------------------
หาคอร์ทแบด "ติดกัน N คอร์ท" ที่ว่างพร้อมกันต่อเนื่อง 2 ชม.
เฉพาะ ศุกร์/เสาร์/อาทิตย์ ของสัปดาห์ถัดไป ตลอดเวลาทำการของสนาม

สนาม: สนามแบดมินตันพิริยะ (providerId=231) ผ่าน matchday backend API

ตั้งใจให้รันเป็น scheduled task ทุกวัน ศุกร์/เสาร์/อาทิตย์
  - รอบวันศุกร์      = สร้าง baseline ของสัปดาห์ถัดไป
  - รอบเสาร์/อาทิตย์ = ดึงซ้ำเพื่ออัปเดต แล้วเทียบว่ามีอะไรหายไป/ว่างเพิ่ม

ตัวอย่าง
--------
    python find_court_pairs.py
    python find_court_pairs.py --window 15:00-20:00   # จำกัดเวลาถ้าอยาก
    python find_court_pairs.py --target-friday 2026-08-14
    python find_court_pairs.py --dump-raw 2026-08-08
    python find_court_pairs.py --from-file day.json --for-date 2026-08-08

หมายเหตุ: อ่านอย่างเดียว ไม่จองอะไร และหน่วงเวลาระหว่าง request
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time as _time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

try:
    import requests
except ImportError:
    requests = None


# ─────────────────────────────── CONFIG ────────────────────────────────

PROVIDER_ID = 231                       # สนามแบดมินตันพิริยะ
API_URL = "https://app2.matchday-backend.com/api/provider-matches"

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "th-TH,th;q=0.9",
    "Origin": "https://portal.matchday.co.th",
    "Referer": "https://portal.matchday.co.th/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
}

REQUEST_DELAY = 1.0                     # วินาที ระหว่างแต่ละ request

# matchday เก็บเวลาเริ่มคิวเป็น HH:01 เพื่อกันคิวชนกัน
# (14:01-16:00 = จองจริง 14:00-16:00) ถ้าไม่ปรับจะเกิดช่องว่างปลอม 1 นาที
SNAP_TOLERANCE_MIN = 5

# เวลาเปิด-ปิดของแต่ละ courtType ต่อวัน (parse_day เติม) ใช้ตอนส่งออก JSON
DAY_HOURS: dict = {}

TH_DAY = {0: "จันทร์", 1: "อังคาร", 2: "พุธ", 3: "พฤหัส", 4: "ศุกร์", 5: "เสาร์", 6: "อาทิตย์"}


# ────────────────────────── ช่วงเวลา (Interval) ──────────────────────────

@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    items = sorted(intervals, key=lambda i: (i.start, i.end))
    if not items:
        return []
    out = [items[0]]
    for iv in items[1:]:
        if iv.start <= out[-1].end:
            if iv.end > out[-1].end:
                out[-1] = Interval(out[-1].start, iv.end)
        else:
            out.append(iv)
    return out


def complement(busy: Iterable[Interval], lo: datetime, hi: datetime) -> list[Interval]:
    free, cursor = [], lo
    for iv in merge(busy):
        if iv.end <= lo or iv.start >= hi:
            continue
        s, e = max(iv.start, lo), min(iv.end, hi)
        if s > cursor:
            free.append(Interval(cursor, s))
        cursor = max(cursor, e)
    if cursor < hi:
        free.append(Interval(cursor, hi))
    return free


def intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """ตัดกันของช่วงเวลาสองชุด — หัวใจของการหา 'คอร์ทติดกันว่างพร้อมกัน'"""
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i].start, b[j].start), min(a[i].end, b[j].end)
        if s < e:
            out.append(Interval(s, e))
        if a[i].end < b[j].end:
            i += 1
        else:
            j += 1
    return out


def intersect_all(groups: list[list[Interval]]) -> list[Interval]:
    if not groups:
        return []
    acc = merge(groups[0])
    for g in groups[1:]:
        acc = intersect(acc, merge(g))
        if not acc:
            break
    return acc


def clip(intervals: Iterable[Interval], lo: datetime, hi: datetime) -> list[Interval]:
    out = []
    for iv in intervals:
        s, e = max(iv.start, lo), min(iv.end, hi)
        if s < e:
            out.append(Interval(s, e))
    return out


def start_times(block: Interval, duration: int, step: int) -> list[datetime]:
    """เวลาเริ่มที่เป็นไปได้ทั้งหมดภายในบล็อกว่างหนึ่งก้อน"""
    need, stp = timedelta(minutes=duration), timedelta(minutes=step)
    midnight = block.start.replace(hour=0, minute=0, second=0, microsecond=0)
    off = (block.start - midnight).total_seconds() / 60
    t = max(midnight + timedelta(minutes=(-(-off // step)) * step), block.start)
    out = []
    while t + need <= block.end:
        out.append(t)
        t += stp
    return out


# ────────────────────────────── คอร์ทและผัง ──────────────────────────────

@dataclass(frozen=True, order=True)
class Court:
    group: str          # ชื่อ courtType เช่น "พื้นยาง(รองเท้าแบดเท่านั้น)"
    number: int         # เลขคอร์ท ใช้ตัดสินว่าติดกันไหม
    raw: str            # ชื่อเดิมจาก API

    def __str__(self) -> str:
        return self.raw


def adjacent_sets(courts: Iterable[Court], size: int,
                  layout: dict[str, list[str]] | None) -> list[tuple[Court, ...]]:
    """
    ชุดคอร์ทที่อยู่ติดกันทางกายภาพ ชุดละ `size` คอร์ท

    default: เลขต่างกัน 1 และอยู่ courtType เดียวกัน = ติดกัน
    ดังนั้นคอร์ท 6 (พื้นยาง) กับคอร์ท 7 (พื้นมาสติก) จะไม่ถูกจับคู่
    ถ้าผังจริงไม่เรียงตามเลข ใช้ --layout ระบุเอง
    """
    by_group: dict[str, list[Court]] = defaultdict(list)
    for c in courts:
        by_group[c.group].append(c)

    result: list[tuple[Court, ...]] = []
    for group, members in by_group.items():
        if layout and group in layout:
            order = {n: i for i, n in enumerate(layout[group])}
            chain = sorted((c for c in members if c.raw in order), key=lambda c: order[c.raw])
            pos = [order[c.raw] for c in chain]
        else:
            chain = sorted((c for c in members if c.number >= 0), key=lambda c: c.number)
            pos = [c.number for c in chain]

        for i in range(len(chain) - size + 1):
            if all(pos[i + k + 1] - pos[i + k] == 1 for k in range(size - 1)):
                result.append(tuple(chain[i:i + size]))
    return result


def parse_layout(spec: str) -> dict[str, list[str]]:
    """รูปแบบ: "ประเภท=คอร์ท 1,คอร์ท 2;ประเภท2=คอร์ท 7,คอร์ท 8" """
    out: dict[str, list[str]] = {}
    for chunk in spec.split(";"):
        if "=" not in chunk:
            continue
        g, names = chunk.split("=", 1)
        out[g.strip()] = [n.strip() for n in names.split(",") if n.strip()]
    return out


# ──────────────────────── parser ของ matchday API ────────────────────────

_NUM = re.compile(r"(\d+)")


def parse_clock(value: str, day: date) -> datetime:
    """รองรับ '13:00' และ '24:00' (= เที่ยงคืนของวันถัดไป)"""
    h, m = (int(x) for x in str(value).split(":")[:2])
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=m)


def snap_start(dt: datetime) -> datetime:
    """
    ปัดเวลาเริ่มคิวลงให้ตรงกริด — matchday เก็บเป็น HH:01
    ถ้าไม่ปัด คิวที่ต่อกันจริง (14:01-16:00 กับ 16:01-18:00)
    จะเหลือ 'ช่องว่าง' 1 นาทีที่ไม่มีอยู่จริง
    """
    off = dt.minute % 30
    return dt - timedelta(minutes=off) if 0 < off <= SNAP_TOLERANCE_MIN else dt


def parse_day(payload: dict, day: date) -> dict[Court, list[Interval]]:
    """คืน dict: Court -> ช่วงเวลาที่ว่างของวันนั้น"""
    if payload.get("isClosed"):
        print(f"  [i] {day}: สนามปิด", file=sys.stderr)
        return {}

    courts: dict[int, Court] = {}
    hours: dict[int, tuple[datetime, datetime]] = {}

    for ctype in payload.get("courtTypes", []):
        gname = str(ctype.get("name", "-")).strip()
        # เวลาเปิดปิดระดับ courtType สำคัญกว่าของสนามรวม
        opn = parse_clock(ctype.get("openTime") or payload.get("providerOpenTime", "00:00"), day)
        cls = parse_clock(ctype.get("closeTime") or payload.get("providerCloseTime", "24:00"), day)
        DAY_HOURS.setdefault(day, {})[gname] = (opn, cls)
        for c in ctype.get("courts", []):
            cid = int(c["id"])
            nums = _NUM.findall(str(c.get("name", "")))
            courts[cid] = Court(group=gname,
                                number=int(nums[-1]) if nums else -1,
                                raw=str(c.get("name", cid)).strip())
            hours[cid] = (opn, cls)

    if not courts:
        print(f"  [!] {day}: ไม่พบคอร์ทใน courtTypes", file=sys.stderr)
        return {}

    busy: dict[int, list[Interval]] = defaultdict(list)
    skipped = 0
    for m in payload.get("matches", []):
        cid = int(m.get("court_id", -1))
        if cid not in courts:
            skipped += 1
            continue
        try:
            s = snap_start(datetime.strptime(m["time_start"], "%Y-%m-%d %H:%M:%S"))
            e = datetime.strptime(m["time_end"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            skipped += 1
            continue
        if e <= s:
            continue
        if s.date() != day and e.date() != day:   # คิวของวันอื่นปนมา
            skipped += 1
            continue
        busy[cid].append(Interval(s, e))

    if skipped:
        print(f"  [i] {day}: ข้าม {skipped} รายการที่จับคู่คอร์ทไม่ได้", file=sys.stderr)

    return {courts[cid]: complement(busy.get(cid, []), *hours[cid]) for cid in courts}


# ───────────── อ่านข้อมูลแบบบรรทัด (สำหรับรันบน cloud ผ่าน WebFetch) ─────────────
#
# บน cloud สคริปต์ยิง API ตรงไม่ได้ (proxy บล็อก) และ WebFetch ไม่คืน JSON ดิบ
# จึงให้ WebFetch แปลงเป็นบรรทัดรูปแบบตายตัว แล้วสคริปต์ประกอบกลับเป็น payload
#   CLOSED|false
#   COURT|<courtType name>|<openTime>|<closeTime>|<court id>|<court name>
#   MATCH|<court_id>|<YYYY-MM-DD HH:MM:SS>|<YYYY-MM-DD HH:MM:SS>
#   TOTAL|<จำนวนบรรทัด MATCH>
# ถ้าจำนวน MATCH ไม่ตรง TOTAL หรือมีบรรทัดผิดรูปแบบ = error (ห้ามใช้ข้อมูลไม่ครบ)

_CLK = re.compile(r"^\d{1,2}:\d{2}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def payload_from_lines(text: str, source: str = "") -> dict:
    closed, total = False, None
    types: dict[str, dict] = {}
    matches: list[dict] = []
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().strip("`").strip()
        if not line or "|" not in line:
            continue
        parts = [x.strip() for x in line.split("|")]
        tag = parts[0].upper()
        if tag == "CLOSED":
            closed = parts[1].lower() in ("true", "1", "yes")
        elif tag == "TOTAL":
            total = int(parts[1])
        elif tag == "COURT":
            if len(parts) != 6 or not (_CLK.match(parts[2]) and _CLK.match(parts[3])
                                       and parts[4].isdigit()):
                raise ValueError(f"{source}:{ln} บรรทัด COURT ผิดรูปแบบ: {raw!r}")
            ct = types.setdefault(parts[1], {"name": parts[1], "openTime": parts[2],
                                             "closeTime": parts[3], "courts": []})
            ct["courts"].append({"id": int(parts[4]), "name": parts[5]})
        elif tag == "MATCH":
            if len(parts) != 4 or not (parts[1].isdigit() and _TS.match(parts[2])
                                       and _TS.match(parts[3])):
                raise ValueError(f"{source}:{ln} บรรทัด MATCH ผิดรูปแบบ: {raw!r}")
            matches.append({"court_id": int(parts[1]), "time_start": parts[2],
                            "time_end": parts[3]})
    if total is None:
        raise ValueError(f"{source}: ไม่มีบรรทัด TOTAL — ข้อมูลอาจถูกตัด")
    if total != len(matches):
        raise ValueError(f"{source}: MATCH {len(matches)} บรรทัด แต่ TOTAL บอก {total}")
    if not types and not closed:
        raise ValueError(f"{source}: ไม่มีบรรทัด COURT เลย")
    return {"isClosed": closed, "courtTypes": list(types.values()), "matches": matches}


# ─────────────────────────────── การดึงข้อมูล ───────────────────────────────

def fetch_day(day: date, cache: Path | None, lines: Path | None = None) -> dict:
    if lines:
        f = lines / f"{day:%Y-%m-%d}.txt"
        if not f.exists():
            raise FileNotFoundError(f"ไม่มีไฟล์ {f}")
        return payload_from_lines(f.read_text(encoding="utf-8"), str(f))
    key = cache / f"{day:%Y-%m-%d}.json" if cache else None
    if key and key.exists():
        return json.loads(key.read_text(encoding="utf-8"))
    if requests is None:
        sys.exit("ต้องติดตั้ง requests ก่อน:  pip install requests")

    resp = requests.get(API_URL, headers=HEADERS, timeout=20,
                        params={"providerId": PROVIDER_ID, "date": f"{day:%Y-%m-%d}"})
    resp.raise_for_status()
    data = resp.json()
    if key:
        cache.mkdir(parents=True, exist_ok=True)
        key.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _time.sleep(REQUEST_DELAY)
    return data


def next_week_targets(today: date) -> list[date]:
    """
    ศุกร์/เสาร์/อาทิตย์ ของสัปดาห์ถัดไป

    ยึดจาก 'ศุกร์ล่าสุดที่ผ่านมาหรือวันนี้' + 7 วัน
    ทำให้รันวันศุกร์ เสาร์ หรืออาทิตย์ ก็ได้ 3 วันเป้าหมายชุดเดียวกันเสมอ
    รอบเสาร์/อาทิตย์จึงเป็นการอัปเดตของรอบวันศุกร์จริง ๆ
    """
    wd = today.weekday()                       # จ=0 ... ศ=4, ส=5, อา=6
    friday = today - timedelta(days=wd - 4 if wd >= 4 else wd + 3) + timedelta(days=7)
    return [friday, friday + timedelta(days=1), friday + timedelta(days=2)]


# ─────────────────────────────── การแสดงผล ───────────────────────────────

def pair_label(pair: tuple[Court, ...]) -> str:
    return "คอร์ท " + "+".join(str(c.number) if c.number >= 0 else c.raw for c in pair)


def short_group(name: str) -> str:
    return name.split("(")[0].strip() or name


def analyse(day: date, courts: dict[Court, list[Interval]], args,
            layout: dict[str, list[str]] | None) -> list[dict]:
    rows = []
    for pair in adjacent_sets(courts.keys(), args.adjacent, layout):
        common = intersect_all([courts[c] for c in pair])
        # ไม่ระบุ --window = ใช้เวลาทำการของ courtType ที่ complement() คิดไว้แล้ว
        if args.win_from:
            common = clip(common, parse_clock(args.win_from, day),
                          parse_clock(args.win_to, day))
        for block in common:
            if starts := start_times(block, args.duration, args.step):
                rows.append({"pair": pair, "group": pair[0].group,
                             "block": block, "starts": starts})
    return sorted(rows, key=lambda r: (r["block"].start, r["pair"][0].number))


def snapshot(all_rows: dict[date, list[dict]]) -> dict:
    return {f"{d:%Y-%m-%d}": sorted(f"{pair_label(r['pair'])}|{r['block']}" for r in rows)
            for d, rows in all_rows.items()}


def report(all_rows: dict[date, list[dict]], args, prev: dict | None) -> None:
    scope = (f"ช่วง {args.win_from}-{args.win_to}" if args.win_from
             else "ตลอดเวลาทำการของสนาม")
    print("\n" + "═" * 68)
    print(f"  คอร์ทติดกัน {args.adjacent} คอร์ท ว่างต่อเนื่อง {args.duration} นาที   {scope}")
    print("═" * 68)

    for day in sorted(all_rows):
        rows = all_rows[day]
        head = f"\n  {TH_DAY[day.weekday()]} {day:%d/%m/%Y}"
        if not rows:
            print(f"{head}  — ไม่มีคู่คอร์ทติดกันที่ว่างพอ")
            continue
        print(head)
        for r in rows:
            ts = [f"{t:%H:%M}" for t in r["starts"]]
            starts = ", ".join(ts) if len(ts) <= 6 else \
                f"{', '.join(ts[:4])} … {ts[-1]}  ({len(ts)} ช่วง)"
            print(f"     {pair_label(r['pair']):<14} {short_group(r['group']):<10} "
                  f"ว่าง {r['block']}  → เริ่มได้ {starts}")

    if prev is not None:
        now = snapshot(all_rows)
        gone, added = [], []
        for d in sorted(set(now) | set(prev)):
            before, after = set(prev.get(d, [])), set(now.get(d, []))
            gone += [f"{d} {x}" for x in sorted(before - after)]
            added += [f"{d} {x}" for x in sorted(after - before)]
        print("\n" + "─" * 68)
        if gone or added:
            print("  เทียบกับรอบก่อนหน้า")
            for x in gone:
                print(f"     - หายไป   : {x}")
            for x in added:
                print(f"     + ว่างเพิ่ม : {x}")
        else:
            print("  ไม่มีอะไรเปลี่ยนจากรอบก่อนหน้า")

    # ── ตัวเลือกสำหรับ poll ──
    byslot: dict[datetime, list[dict]] = defaultdict(list)
    for rows in all_rows.values():
        for r in rows:
            for t in r["starts"]:
                byslot[t].append(r)
    if not byslot:
        print("\n  ไม่พบช่วงที่ตรงเงื่อนไข — ยังไม่มีอะไรให้โหวต")
        return

    best_per_day: dict[date, datetime] = {}
    for t, rs in byslot.items():
        d = t.date()
        if d not in best_per_day or len(rs) > len(byslot[best_per_day[d]]):
            best_per_day[d] = t
    picks = sorted(best_per_day.values())

    span = timedelta(minutes=args.duration)
    for t in sorted(byslot, key=lambda t: (-len(byslot[t]), t)):
        if len(picks) >= args.top:
            break
        # ตัวเลือกที่คาบเกี่ยวกันเองไม่มีประโยชน์ในการโหวต
        if t not in picks and not any(abs(t - c) < span for c in picks):
            picks.append(t)
    picks = sorted(picks[:args.top])

    print("\n" + "═" * 68)
    print("  ตัวเลือกสำหรับ poll IG")
    print("═" * 68)
    print("\n  ตีแบดสัปดาห์หน้า วันไหนดี? 🏸\n")
    for i, t in enumerate(picks, 1):
        d = t.date()
        pairs = ", ".join(sorted({pair_label(r["pair"]) for r in byslot[t]}))
        print(f"  {i}. {TH_DAY[d.weekday()]} {d.day}/{d.month} {t:%H:%M}-{t + span:%H:%M}")
        print(f"       ({pairs})")
    print("\n  [ IG story: poll sticker = 2 ตัวเลือก / quiz sticker = 4 ]")


# ─────────────────────── ส่งออก JSON สำหรับหน้าเว็บ ───────────────────────

def _clock(dt: datetime, day: date) -> str:
    if dt.date() > day:
        return f"{24 + dt.hour:02d}:{dt.minute:02d}"
    return f"{dt:%H:%M}"


def poll_picks(all_rows: dict[date, list[dict]], args) -> tuple[list[datetime], dict]:
    byslot: dict[datetime, list[dict]] = defaultdict(list)
    for rows in all_rows.values():
        for r in rows:
            for t in r["starts"]:
                byslot[t].append(r)
    if not byslot:
        return [], byslot
    best_per_day: dict[date, datetime] = {}
    for t, rs in byslot.items():
        d = t.date()
        if d not in best_per_day or len(rs) > len(byslot[best_per_day[d]]):
            best_per_day[d] = t
    picks = sorted(best_per_day.values())
    span = timedelta(minutes=args.duration)
    for t in sorted(byslot, key=lambda t: (-len(byslot[t]), t)):
        if len(picks) >= args.top:
            break
        if t not in picks and not any(abs(t - c) < span for c in picks):
            picks.append(t)
    return sorted(picks[:args.top]), byslot


def build_json(days: list[date], all_rows: dict[date, list[dict]],
               courts_by_day: dict[date, dict], errors: dict[date, str],
               args, layout, prev: dict | None) -> dict:
    out_days = []
    for d in days:
        item = {"date": f"{d:%Y-%m-%d}", "dow": TH_DAY[d.weekday()],
                "label": f"{d.day}/{d.month}", "ok": d in all_rows,
                "error": errors.get(d), "closed": False, "groups": [], "slots": []}
        courts = courts_by_day.get(d)
        if d in all_rows and not courts:
            item["closed"] = True
        if courts:
            hours = DAY_HOURS.get(d, {})
            rows_by_pair = defaultdict(list)
            for r in all_rows.get(d, []):
                rows_by_pair[r["pair"]].append(r)
            groups: dict[str, dict] = {}
            for pair in adjacent_sets(courts.keys(), args.adjacent, layout):
                g = pair[0].group
                if g not in groups:
                    o, c = hours.get(g, (None, None))
                    groups[g] = {"name": short_group(g), "full": g,
                                 "open": _clock(o, d) if o else None,
                                 "close": _clock(c, d) if c else None, "pairs": []}
                groups[g]["pairs"].append({
                    "label": "+".join(str(c.number) for c in pair),
                    "blocks": [{"from": _clock(r["block"].start, d),
                                "to": _clock(r["block"].end, d),
                                "starts": [_clock(t, d) for t in r["starts"]]}
                               for r in sorted(rows_by_pair.get(pair, []),
                                               key=lambda r: r["block"].start)]})
            item["groups"] = list(groups.values())
            opens = [g["open"] for g in item["groups"] if g["open"]]
            closes = [g["close"] for g in item["groups"] if g["close"]]
            item["open"] = min(opens) if opens else None
            item["close"] = max(closes) if closes else None
            slot: dict[datetime, list[str]] = defaultdict(list)
            for r in all_rows.get(d, []):
                for t in r["starts"]:
                    slot[t].append(pair_label(r["pair"]).replace("คอร์ท ", ""))
            item["slots"] = [{"start": _clock(t, d),
                              "end": _clock(t + timedelta(minutes=args.duration), d),
                              "pairs": sorted(set(v), key=lambda x: int(x.split("+")[0]))}
                             for t, v in sorted(slot.items())]
        out_days.append(item)

    changes = None
    if prev is not None:
        now = snapshot(all_rows)
        gone, added = [], []
        for ds in sorted(set(now) | set(prev)):
            if ds not in now:          # วันนี้ดึงไม่สำเร็จ ห้ามสรุปว่าหายไป
                continue
            before, after = set(prev.get(ds, [])), set(now.get(ds, []))
            for kind, bag in (("gone", before - after), ("added", after - before)):
                for x in sorted(bag):
                    pl, blk = x.split("|")
                    (gone if kind == "gone" else added).append(
                        {"date": ds, "pair": pl.replace("คอร์ท ", ""), "block": blk})
        changes = {"gone": gone, "added": added}

    picks, byslot = poll_picks(all_rows, args)
    span = timedelta(minutes=args.duration)
    poll = [{"date": f"{t:%Y-%m-%d}", "dow": TH_DAY[t.weekday()],
             "label": f"{t.day}/{t.month}", "start": f"{t:%H:%M}",
             "end": _clock(t + span, t.date()),
             "pairs": sorted({pair_label(r["pair"]).replace("คอร์ท ", "") for r in byslot[t]},
                             key=lambda x: int(x.split("+")[0]))}
            for t in picks]
    return {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "adjacent": args.adjacent, "duration": args.duration,
            "target": f"{days[0]:%Y-%m-%d}", "days": out_days,
            "changes": changes, "poll": poll}


# ──────────────────────────────── main ────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="หาคอร์ทแบดติดกันที่ว่างพร้อมกัน (matchday)")
    p.add_argument("--adjacent", type=int, default=2, help="ต้องติดกันกี่คอร์ท")
    p.add_argument("--duration", type=int, default=120, help="นาที")
    p.add_argument("--step", type=int, default=30, help="ความละเอียดเวลาเริ่ม (นาที)")
    p.add_argument("--window", default=None,
                   help="จำกัดช่วงเวลา เช่น 15:00-20:00 (ไม่ใส่ = ทั้งเวลาทำการของสนาม)")
    p.add_argument("--layout", help='ผังคอร์ทจริง "ประเภท=คอร์ท 1,คอร์ท 2;..."')
    p.add_argument("--top", type=int, default=4, help="จำนวนตัวเลือกใน poll")
    p.add_argument("--target-friday", help="ระบุวันศุกร์เป้าหมายเอง YYYY-MM-DD")
    p.add_argument("--only-on", default="4,5,6",
                   help="รันเฉพาะวันเหล่านี้ (0=จ..6=อา) ใส่ 'any' เพื่อรันทุกวัน")
    p.add_argument("--state", type=Path, default=Path("last_run.json"),
                   help="ไฟล์เก็บผลรอบก่อน ไว้เทียบว่ามีอะไรเปลี่ยน")
    p.add_argument("--cache", type=Path, default=None, help="โฟลเดอร์ cache (debug)")
    p.add_argument("--lines", type=Path, default=None,
                   help="โฟลเดอร์ไฟล์ <YYYY-MM-DD>.txt รูปแบบบรรทัด COURT/MATCH (โหมด cloud)")
    p.add_argument("--json", type=Path, default=None,
                   help="เขียนผลแบบมีโครงสร้างลงไฟล์นี้ (ใช้แสดงบนหน้าเว็บ)")
    p.add_argument("--dump-raw", metavar="YYYY-MM-DD")
    p.add_argument("--list-dates", action="store_true",
                   help="พิมพ์วันเป้าหมายและ URL ที่ต้องดึง แล้วจบ (ไม่ยิงเน็ต)")
    p.add_argument("--from-file", type=Path)
    p.add_argument("--for-date", help="ใช้คู่กับ --from-file")
    args = p.parse_args()

    args.win_from, args.win_to = (args.window.split("-") if args.window else (None, None))
    layout = parse_layout(args.layout) if args.layout else None

    if args.list_dates:
        today = date.today()
        if args.target_friday:
            fri = datetime.strptime(args.target_friday, "%Y-%m-%d").date()
            days = [fri + timedelta(days=i) for i in range(3)]
        else:
            days = next_week_targets(today)
        print(f"# วันนี้ {TH_DAY[today.weekday()]} {today:%Y-%m-%d}")
        print(f"# ต้องดึง 3 วัน แล้วเซฟเป็น <โฟลเดอร์cache>/<YYYY-MM-DD>.json")
        for d in days:
            print(f"{d:%Y-%m-%d}\t{API_URL}?providerId={PROVIDER_ID}&date={d:%Y-%m-%d}")
        return

    if args.dump_raw:
        d = datetime.strptime(args.dump_raw, "%Y-%m-%d").date()
        print(json.dumps(fetch_day(d, args.cache), ensure_ascii=False, indent=2))
        return

    if args.from_file:
        d = (datetime.strptime(args.for_date, "%Y-%m-%d").date()
             if args.for_date else date.today())
        courts = parse_day(json.loads(args.from_file.read_text(encoding="utf-8")), d)
        print(f"อ่านได้ {len(courts)} คอร์ท")
        for c in sorted(courts):
            print(f"  {c.raw:<10} [{short_group(c.group)}]  "
                  f"ว่าง: {', '.join(map(str, courts[c])) or '-'}")
        print("\nคู่ที่ติดกัน:", ", ".join(pair_label(x) for x in
                                          adjacent_sets(courts.keys(), args.adjacent, layout)))
        report({d: analyse(d, courts, args, layout)}, args, None)
        return

    today = date.today()
    if args.only_on != "any" and today.weekday() not in {int(x) for x in args.only_on.split(",")}:
        print(f"วันนี้ ({TH_DAY[today.weekday()]}) ไม่อยู่ในรอบที่ตั้งไว้ — ข้าม")
        return

    if args.target_friday:
        fri = datetime.strptime(args.target_friday, "%Y-%m-%d").date()
        days = [fri, fri + timedelta(days=1), fri + timedelta(days=2)]
    else:
        days = next_week_targets(today)

    print(f"รันวัน {TH_DAY[today.weekday()]} {today:%d/%m} → "
          f"ดึงข้อมูล {days[0]:%d/%m} - {days[-1]:%d/%m}")

    all_rows: dict[date, list[dict]] = {}
    courts_by_day: dict[date, dict] = {}
    errors: dict[date, str] = {}
    for d in days:
        try:
            courts_by_day[d] = parse_day(fetch_day(d, args.cache, args.lines), d)
            all_rows[d] = analyse(d, courts_by_day[d], args, layout)
        except Exception as exc:
            errors[d] = f"{type(exc).__name__}: {exc}"
            print(f"  [!] {d}: {type(exc).__name__}: {exc}", file=sys.stderr)

    if not all_rows:
        sys.exit("ดึงข้อมูลไม่สำเร็จสักวัน — ห้ามเดาข้อมูลเอง ให้ตรวจ API ก่อน")
    if len(all_rows) < len(days):
        print("  [!] ข้อมูลไม่ครบทุกวัน — ไม่บันทึก state รอบนี้", file=sys.stderr)
    save_state = len(all_rows) == len(days)

    prev = None
    if args.state and args.state.exists():
        try:
            saved = json.loads(args.state.read_text(encoding="utf-8"))
            if saved.get("target") == f"{days[0]:%Y-%m-%d}":   # เทียบเฉพาะเป้าหมายเดียวกัน
                prev = saved.get("rows")
        except (json.JSONDecodeError, OSError):
            pass

    report(all_rows, args, prev)

    if args.json:
        args.json.write_text(json.dumps(
            build_json(days, all_rows, courts_by_day, errors, args, layout, prev),
            ensure_ascii=False, indent=1), encoding="utf-8")

    if args.state and save_state:
        args.state.write_text(json.dumps(
            {"target": f"{days[0]:%Y-%m-%d}", "ran_at": datetime.now().isoformat(),
             "rows": snapshot(all_rows)}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
