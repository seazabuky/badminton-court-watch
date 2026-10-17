# court-watch

หาคอร์ทแบดติดกัน 2 คอร์ทที่ว่างพร้อมกันต่อเนื่อง 2 ชม. ที่สนามแบดมินตันพิริยะ
สำหรับวันศุกร์ เสาร์ และอาทิตย์ของสัปดาห์ถัดไป

- **หน้าเว็บ**: https://seazabuky.github.io/badminton-court-watch/ (`index.html` อ่าน `latest.json`)
- **อัปเดตอัตโนมัติ**: workflow `Check courts` รันทุกวันศุกร์ เสาร์ อาทิตย์ ราว 11:07 เวลาไทย
  แล้ว commit `latest.json` กับ `state.json` กลับเข้า repo
- **รันเองทันที**: แท็บ Actions → Check courts → Run workflow

## ไฟล์

| ไฟล์ | หน้าที่ |
|---|---|
| `find_court_pairs.py` | ดึงข้อมูลจาก matchday แล้วหาคู่คอร์ทติดกันที่ว่าง (อ่านอย่างเดียว ไม่จอง) |
| `latest.json` | ผลรอบล่าสุด ใช้แสดงบนหน้าเว็บ |
| `state.json` | ผลรอบก่อน ใช้เทียบว่ามีอะไรหายไปหรือว่างเพิ่ม |
| `message.txt` | ข้อความสรุปรอบล่าสุด (workflow สร้างให้) ที่ Claude ส่งเข้ามือถือ |
| `notify_text.py` | สรุป `latest.json` เป็นข้อความสั้น (เริ่มเล่น 15:00–19:00 แยกพื้นยาง/มาสติก เป็นรอบ 2 ชม. ไม่ซ้อนกัน) สำหรับแจ้งเตือนบนมือถือ |

## รันบนเครื่อง

```bash
pip install requests
python find_court_pairs.py --only-on any --state state.json --json latest.json
python -m http.server   # แล้วเปิด http://localhost:8000
```
