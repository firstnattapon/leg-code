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

## Webull LEGO Chain — one new row (19 แท็บ)

`lego_dashboard.py` เป็น entry point แยกจาก Dashboard และ Manual เดิม **หนึ่ง run
สร้าง row ใหม่เพียงแถวเดียว** ไม่ใช่ดึงประวัติหลายแถวมาเติมคอลัมน์:

1. **Tab 0** — กด Connect & Load เพื่ออ่าน Webull **snapshot ชุดเดียว**
   (Account list, Balance, Positions, Quote) และอ่าน **latest anchor** ของ chain
   เดียวกันจาก `webull_lego_state` เท่านั้น **ไม่แตะ `shannon_demon_trades`** แล้ว
   สร้าง draft row 1 แถวพร้อม deterministic `run_id`
2. **Tab 1–17** — กด Run เพื่อเปิดเผยคอลัมน์ทีละคอลัมน์ของ row เดียวนั้น
   (holdings/price จาก snapshot, decision object ครั้งเดียวที่ Step 8, price-path
   recurrence ที่ Step 14–17)
3. **Tab 18** — ตรวจ 17 คอลัมน์แล้ว **append แบบ transaction**: idempotent เมื่อ
   `run_id` เดิม และ fail-closed เมื่อ anchor ล้าสมัย จากนั้นจึงเปิด order panel

หลัก calculation (Step 1–17) อยู่ใน **engine เดียว** `lego_one_row.py`; การ append
transaction อยู่ใน `lego_state.py` (`webull_lego_rows` / `webull_lego_state` /
`webull_lego_order_audit`)

- [LEARNING_GUIDE.md](LEARNING_GUIDE.md) อธิบาย DNA seed+mutation, snapshot และ recurrence
- Sidebar `Run ALL 0 → 18` อ่าน snapshot ใหม่ + คำนวณ 1 แถว + append ในคลิกเดียว ให้ผล
  row เดียวกับ Manual 0→18 ทุกประการ (parity-tested)
- [webull_lego_single_file.py](webull_lego_single_file.py) เป็นไฟล์เดียว read-only ที่
  คำนวณ row เดียวด้วยสูตรเดียวกับ engine (ไม่มี place/cancel และไม่ append)

```bash
.venv/Scripts/python -m streamlit run lego_dashboard.py
```

### สูตรของแถวใหม่

- **แถวแรกของ chain (genesis)** — `P₀ = Pₙ`, `DNA step = 0`, และ
  `R₀ = ΔA₀ = A₀ = E₀ = 0`
- **แถวถัดไป** ใช้ anchor ล่าสุดเพียงแถวเดียว:
  `Rₙ = FIX_C·ln(Pₙ/P₀)`, `ΔAₙ = FIX_C·(Pₙ/Pₙ₋₁−1)`,
  `Aₙ = Aₙ₋₁ + ΔAₙ`, `Eₙ = Aₙ − Rₙ` — `P₀`, `Pₙ₋₁`, `Aₙ₋₁` มาจาก anchor
- **Decision (Step 8)** สร้างครั้งเดียวแล้วเปิดเผยที่ Step 9–13:
  `gap = FIX_C − holdings×price`; ถ้า DNA signal = 0 → `PASS_DNA_ZERO`,
  ถ้า `|gap| ≤ DIFF` → `PASS_THRESHOLD`, ถ้า `gap > DIFF` → `READY_BUY`,
  ถ้า `gap < −DIFF` → `READY_SELL`, `quantity = round(|gap|/price, decimal_precision)`
- ค่าเงินคำนวณ full precision และ round เป็น 2 ตำแหน่งเฉพาะตอนแสดง/ดาวน์โหลด

### Streamlit secrets

คัดลอก schema จาก `.streamlit/secrets.example.toml` ไปใส่ใน App settings > Secrets
และแทน placeholder ด้วย Firebase service account จริง ห้าม commit `secrets.toml`,
Webull Account ID, App Key หรือ App Secret

Webull credentials ถูกกรอกในหน้าแอปและอยู่เฉพาะ Streamlit session ส่วนค่าคงที่ใช้:

```toml
[lego_dashboard]
fix_c = 1500.0
diff = 0.0
dna_code = "bypass:100"
decimal_precision = 5
order_audit_collection = "webull_lego_order_audit"
audit_to_firestore = false
```

การเปลี่ยน `fix_c`, `diff`, `dna_code` หรือ `decimal_precision` จะ **เริ่ม chain ใหม่**
(สร้าง `chain_key`/`webull_lego_state` key ใหม่) เพราะเปลี่ยนวิธีคำนวณทุกคอลัมน์
Step 18 เขียน `webull_lego_rows`/`webull_lego_state` แบบ transaction ส่วน order audit
เก็บใน session และดาวน์โหลดได้ที่ Tab 18; ตั้ง `audit_to_firestore = true` เฉพาะเมื่อ
service account มีสิทธิ์เขียน `webull_lego_order_audit`

### Deploy บน Streamlit Community Cloud

1. สร้างแอปจาก repository นี้และตั้ง Main file path เป็น `lego_dashboard.py`
2. จำกัด viewer เป็น **Private single-user** ก่อนเพิ่ม secrets
3. วาง Firebase secrets ตามไฟล์ตัวอย่าง และให้ service account อ่าน `webull_lego_state`
   และเขียน `webull_lego_rows`/`webull_lego_state` ได้ (append Step 18)
4. เปิดแอปและยืนยันว่า Environment เริ่มต้นเป็น `Test (UAT)`
5. ทดสอบ Connect & Load, Run 1–17, Append final row (Step 18) และ download final row CSV/JSON
6. Order panel อยู่ **หลัง Step 18** เท่านั้น: เปิดเฉพาะ UAT และเฉพาะ final row ที่เป็น
   `READY_BUY`/`READY_SELL` — Production เป็น read-only แบบ fail-closed

Order panel ใช้ payload จาก final row ที่บันทึกแล้ว (`client_order_id` เป็น deterministic
จาก `run_id`) ต้อง Preview payload เดิมก่อน Submit และพิมพ์ confirmation phrase ให้ตรง
สถานะ `SUBMITTED`/`PENDING` จะไม่ถูกนับเป็น `FILLED` และผลที่เขียนลง
`webull_lego_order_audit` ถูก redact ไม่มี credentials/raw account response

หน้า Manual ใช้งานได้โดยไม่ต้องตั้งค่า Firestore ส่วนหน้า Dashboard ต้องมี
`.streamlit/secrets.toml` ที่ประกอบด้วย `firebase_service_account`

## Price-path recurrence contract

คอลัมน์ `Rₙ`, `ΔAₙ`, `Aₙ`, `Eₙ` ของ LEGO Chain เป็น **price-path recurrence** จาก
current quote (`Pₙ`) และ anchor ล่าสุดเพียงแถวเดียว (`P₀`, `Pₙ₋₁`, `Aₙ₋₁`) ไม่ใช่
broker execution ledger และไม่สแกนประวัติ fill — order lifecycle (Preview/Submit
result) ถูกแยกเก็บไว้ใน `webull_lego_order_audit` ต่างหากจากสถานะผลการคำนวณ

## Security

- ค่าเริ่มต้นของ Manual และ LEGO Chain คือ **Test (UAT)**
- Account ID, App Key และ App Secret ต้องกรอกขณะใช้งานและไม่ถูกเขียนลงไฟล์ (อยู่ใน
  session เท่านั้น ไม่เขียนลง final row/audit/download/log)
- ห้าม commit `.streamlit/secrets.toml`, `.env` หรือ credentials ใด ๆ
- Order panel อยู่หลัง Step 18 และเปิดเฉพาะ **UAT** + final row ที่เป็น READY_BUY/READY_SELL
  การส่งคำสั่งต้องกด Submit เองหลัง Preview เสมอ; **Production เป็น read-only** แบบ fail-closed
- Credential ที่เคยส่งผ่านแชตหรือช่องทางสาธารณะควรถูก revoke/rotate

## Test

```bash
.venv/Scripts/python -m pytest -q
```
