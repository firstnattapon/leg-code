# Webull Dashboard

Streamlit dashboard สำหรับดู Shannon Demon state/trades และหน้า **Manual Test
Lab** สำหรับทดสอบ Webull, DNA, Logical FIX_C และ benchmark แบบเจาะจง

Manual Test Lab รองรับ connection/quote, account list, balance, positions,
order preview/place, open orders, history, detail, cancel, DNA encode/decode,
Logical FIX_C, local benchmark และแท็บ Web Apps ที่รวมคู่มือ Rebalancing 101
กับ Rebalancing Playground แบบโต้ตอบไว้ในหน้าเดียว รวมถึงแท็บ **Cheat Sheet**
สำหรับทดลองสูตร BUY/SELL/PASS และ price-path Aₙ/Rₙ/Eₙ แบบรวดเร็ว

Cheat Sheet เป็น educational/what-if calculator ที่ทำงานจากไฟล์ local และไม่เรียก
Webull API ตัวเลข Aₙ/Rₙ/Eₙ ในหน้านี้จึงไม่ใช่ realized broker ledger; การสรุป
เงินจริงต้องใช้ quantity, execution price และ position reconciliation จาก fill จริง

## Run locally

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m streamlit run streamlit_dashboard.py
```

## Webull LEGO Chain — แอปใหม่ 19 แท็บ

`lego_dashboard.py` เป็น entry point แยกจาก Dashboard และ Manual เดิม ผู้ใช้ต้อง
ยืนยัน Webull + Firestore ที่ Tab 0 แล้วกด Run เองทีละ Step 1–17 ก่อนเปิด Final
DataFrame ที่ Tab 18

- [LEARNING_GUIDE.md](LEARNING_GUIDE.md) อธิบาย DNA seed+mutation, API จริง และ ledger
- Sidebar `Run ALL 0 → 18 (REAL READ)` ยิง Account/Balance/Positions/Quote + Firestore
  ใหม่จริง แล้วต่อครบทุกขั้นในคลิกเดียว (loop เป็น read-only)
- หลัง All-in สำเร็จ Sidebar จะมี **All-in REAL order** panel ที่ยิง Webull Order API จริง
  จาก final decision ของ chain โดยใช้ submit gate เดียวกับ Manual Run: ต้อง Preview →
  พิมพ์ confirmation phrase → (Production) เปิด safety switch → กด Submit เอง
- [webull_lego_single_file.py](webull_lego_single_file.py) เป็นไฟล์เดียวที่รัน 0→18
  ได้โดยไม่ import โมดูลในโปรเจกต์ และรองรับ Test/Production read-only

```bash
.venv/Scripts/python -m streamlit run lego_dashboard.py
```

ลำดับของแต่ละแท็บเหมือน LEGO block: Goal → Quick Start → แสดง **Single-File
Python source** → Run → validate → แสดง accumulated DataFrame → ส่งต่อขั้นถัดไป
→ อ่าน Learning Guide โดยไม่มี `exec` หรือโค้ดที่สร้างจากข้อความผู้ใช้

Block 1–17 อยู่ใน `lego_blocks/step_*.py` แต่ละไฟล์จบในตัวเองและไม่ import โมดูล
ภายในโปรเจกต์ ผู้ใช้กด **Download Single-File LEGO Block** แล้วรัน CLI ตามคำสั่ง
Quick Start ที่อยู่ใน docstring บนสุดได้ทันที ฟังก์ชัน `transform` ในไฟล์ที่แสดงคือ
callable เดียวกับที่ปุ่ม Run ของ Streamlit เรียก จึงไม่มีโค้ดตัวอย่างคนละชุดกับโค้ดจริง

Final DataFrame มี 17 คอลัมน์ตาม contract และแยกข้อมูลเงินสองชนิดออกจากกัน:

- `Rₙ`, `ΔAₙ`, `Aₙ`, `Eₙ` ในตารางหลักเป็น broker-confirmed execution ledger
- What-if table คำนวณทุก positive quote เพื่อการเรียนรู้/เทียบ CSV เท่านั้น

### Streamlit secrets

คัดลอก schema จาก `.streamlit/secrets.example.toml` ไปใส่ใน App settings > Secrets
และแทน placeholder ด้วย Firebase service account จริง ห้าม commit `secrets.toml`,
Webull Account ID, App Key หรือ App Secret

Webull credentials ถูกกรอกในหน้าแอปและอยู่เฉพาะ Streamlit session ส่วนค่าคงที่ใช้:

```toml
[lego_dashboard]
trade_collection = "shannon_demon_trades"
audit_collection = "webull_lego_uat_audit"
trade_limit = 100
fix_c = 1500.0
```

### Deploy บน Streamlit Community Cloud

1. สร้างแอปจาก repository นี้และตั้ง Main file path เป็น `lego_dashboard.py`
2. จำกัด viewer เป็น **Private single-user** ก่อนเพิ่ม secrets
3. วาง Firebase secrets ตามไฟล์ตัวอย่าง และให้ service account มีสิทธิ์อ่าน trade
   collection/เขียน audit collection เท่าที่จำเป็น
4. เปิดแอปและยืนยันว่า Environment เริ่มต้นเป็น `Test (UAT)`
5. ทดสอบ Connect & Load, Run 1–17, order panel (Preview → Place → Query) และ download Final/What-if
6. UAT ยิง UAT endpoint จริง; Production ส่ง order เงินจริงได้เฉพาะเมื่อเปิด safety switch
   และพิมพ์ confirmation phrase ที่ทวน account/symbol/side/quantity ให้ตรง

ทุกแท็บ 0–18 มี order panel เดียวกันและแยกจากปุ่ม Run เสมอ: ต้อง Preview payload เดิม
ก่อน Submit, พิมพ์ confirmation phrase ให้ตรง และ (Production) เปิด safety switch ก่อนยิง
`place_order` จริง จากนั้นใช้ Query อ่านสถานะจริง สถานะ `SUBMITTED`/`PENDING` จะไม่ถูกนับเป็น
`FILLED` และผลที่เขียนลง `webull_lego_uat_audit` ถูก redact ไม่มี credentials/raw account response

แผน machine-readable และคู่มือ offline อยู่ที่ `webull_lego_chain_plan.json` กับ
`webull_lego_chain_guide.html`

หน้า Manual ใช้งานได้โดยไม่ต้องตั้งค่า Firestore ส่วนหน้า Dashboard ต้องมี
`.streamlit/secrets.toml` ที่ประกอบด้วย `firebase_service_account`

## Realized cash-flow contract

ตาราง trade log แยกข้อมูลสองชนิดออกจากกัน:

- กราฟ Learning Guide เป็น **what-if price path** จาก market quote
- คอลัมน์ `ΔAₙ`, `Aₙ` และ `Eₙ` เป็น **realized execution ledger** และรับข้อมูล
  เฉพาะ terminal fill ที่มี `filled_quantity > 0`, `position_reconciled = true`,
  side เป็น BUY/SELL และมี execution/average fill price จริง

`PASS`, pending, rejected, unfilled และ `ORDER_*_POSITION_PENDING` ไม่ทำให้ยอด
realized เปลี่ยน และ dashboard จะไม่ใช้ `last_price` แทน execution price หาก
trade document ยังไม่มีราคาที่ execute คอลัมน์ realized จะเว้นว่างพร้อมคำเตือน
เพื่อไม่สร้างตัวเลขเงินจริงที่พิสูจน์ไม่ได้ ค่า `Aₙ` จะหัก fee เมื่อ log มี field
ค่าธรรมเนียมที่รองรับ มิฉะนั้นจะแสดง gross cash พร้อม contract นี้อย่างชัดเจน

## Security

- ค่าเริ่มต้นของ Manual และ LEGO Chain คือ **Test (UAT)**
- Account ID, App Key และ App Secret ต้องกรอกขณะใช้งานและไม่ถูกเขียนลงไฟล์
- ห้าม commit `.streamlit/secrets.toml`, `.env` หรือ credentials ใด ๆ
- ทุกแท็บ 0–18 มี order panel จริง แต่การส่งคำสั่งต้องกด Submit เองหลัง Preview เสมอ
- Production order ต้องเปิด safety switch และพิมพ์ confirmation phrase (ทวน account/symbol/side/quantity) ให้ตรง
- Credential ที่เคยส่งผ่านแชตหรือช่องทางสาธารณะควรถูก revoke/rotate

## Test

```bash
.venv/Scripts/python -m pytest -q
```
