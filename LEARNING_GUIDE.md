# Learning Guide — Webull LEGO 0→18 (one new row)

อ่านหน้านี้ก่อน 5 นาที แล้วเริ่มได้จาก `lego_dashboard.py` หรือไฟล์เดียว
`webull_lego_single_file.py`

หลักการใหม่: **หนึ่ง run สร้าง row ใหม่เพียงแถวเดียว** จาก Webull snapshot ปัจจุบัน
หนึ่งชุด + anchor ล่าสุดหนึ่งแถวของ chain เดียวกัน ไม่ใช่การดึงประวัติหลายแถวมา
เติมคอลัมน์ย้อนหลัง

หลัก DNA และ early-exit chain อ้างอิงจาก
[Shannon Demon DNA Bot Learning Guide](https://github.com/firstnattapon/webull/blob/main/doc/LEARNING_GUIDE.md)
โดย dashboard นี้นำส่วน decoder และ broker read contract มาใช้ตรง ๆ ส่วน scheduler/
transaction ของ trading bot ยังคงเป็นความรับผิดชอบของ bot ไม่ใช่ analytics app

## Quick Start

### Streamlit

```powershell
python -m pip install -r requirements.txt
python -m streamlit run lego_dashboard.py
```

1. เปิด Tab 0 และเลือก `Test (UAT)` หรือ `Production`
2. ใส่ Account ID, App Key, App Secret, **symbol (จำเป็น)** และ DNA_CODE
3. กด `Connect & Load` เพื่ออ่าน snapshot จริง + anchor ล่าสุด แล้วเดิน Step 1–17
   หรือกด `Run ALL 0 → 18` ที่ sidebar เพื่อ read → compute → append ในคลิกเดียว

### Single File

```powershell
python -m pip install pandas numpy google-cloud-firestore google-auth webull-openapi-python-sdk
$env:WEBULL_ACCOUNT_ID="..."
$env:WEBULL_APP_KEY="..."
$env:WEBULL_APP_SECRET="..."
$env:GOOGLE_APPLICATION_CREDENTIALS="C:\safe\firebase.json"
python webull_lego_single_file.py --environment "Test (UAT)" --symbol AAPL --dna-code "bypass:100"
```

ไฟล์เดียวนี้เป็น read-only: อ่าน snapshot + anchor แล้วคำนวณ row เดียว ไม่มีเมธอด
place/cancel และไม่ append Firestore การ append transaction ทำที่ Streamlit Step 18

## Step 0 อ่านอะไรจริง

แอปใช้ official Webull Python SDK และ endpoint ตาม environment:

| Environment | Endpoint |
|---|---|
| Test (UAT) | `th-api.uat.webullbroker.com` |
| Production | `api.webull.co.th` |

Step 0 อ่าน **snapshot ชุดเดียว** และ anchor ของ chain เท่านั้น:

1. Account list
2. Account balance
3. Account positions → ใช้เป็น `holdings` ของ symbol
4. Market snapshot (quote) → ใช้เป็น `ราคา Pₙ`
5. `webull_lego_state/{chain_key}` → latest anchor (P₀, Pₙ₋₁, Aₙ₋₁, DNA step, version)

**ไม่มีการอ่าน `shannon_demon_trades` หรือประวัติหลายแถว** (`old_trade_log_reads = 0`)
read calls retry ได้สูงสุด 3 ครั้งเมื่อเป็น network/429/5xx แต่ authentication หรือ
validation error จะหยุดทันที Credentials อยู่เฉพาะ session memory และไม่ถูกเขียนลง
final row, JSON download หรือ audit

`chain_key = hash(environment, account fingerprint, symbol, strategy_config_hash)`
โดย `strategy_config_hash` ครอบคลุม strategy id, FIX_C, DIFF, DNA hash และ decimal
precision — เปลี่ยนค่าใดค่าหนึ่งจะเริ่ม chain ใหม่

## DNA decode ที่ถูกหลักการ

DNA ไม่ใช่การสุ่มใหม่ทุกครั้ง แต่เป็น deterministic Hybrid Multi-Mutation sequence
ตาม `DNA_CODE`

### Encoded

ตัวเลขถูกอ่านแบบ `[จำนวนหลัก][ค่า]` ต่อกัน เช่น code ที่ถอดได้เป็น
`[length, mutation_rate, dna_seed, mutation_seed_1, ...]`

```text
1. ใช้ dna_seed สร้าง base array 0/1 ยาว length
2. บังคับ signal แรกเป็น 1
3. สำหรับ mutation seed ทุกตัว สร้าง mask ด้วย mutation_rate
4. flip 0↔1 เฉพาะตำแหน่งใน mask
5. บังคับ signal แรกเป็น 1 หลัง mutation ทุกครั้ง
```

ถ้า mutation rate มากกว่า 1 จะตีความเป็นเปอร์เซ็นต์ เช่น `10` = `0.10`

### Bypass

- `bypass:100`
- `[1,100]`

ทั้งสองแบบหมายถึง sequence เลข 1 จำนวน 100 ขั้น เหมาะกับการทดสอบเท่านั้น

### DNA step และ signal ของแถวใหม่

`DNA step` ของแถวใหม่ = DNA step ของ anchor ล่าสุด + 1 (chain ใหม่เริ่มที่ 0)
`DNA signal` = bit ที่ตำแหน่ง `DNA step` ของ sequence ที่ decode แล้ว เป็น
deterministic ล้วน ถ้า `DNA step` เกินความยาว sequence จะ **fail-closed** ทันที
(DNA exhausted) ไม่เดาค่าและไม่คำนวณต่อ

## LEGO chain ทำงานอย่างไร (row เดียว)

```text
0 snapshot ชุดเดียว (positions + quote) + latest anchor
  → 1 เวลา UTC ของ snapshot
  → 2 สินทรัพย์ (symbol ที่เลือก)
  → 3 สถานะ (SNAPSHOT_READY ระหว่าง draft)
  → 4 DNA step = anchor + 1 หรือ 0
  → 5 DNA signal (deterministic จาก DNA_CODE)
  → 6 ราคา Pₙ (live quote)
  → 7 holdings (live positions)
  → 8 สร้าง decision object ครั้งเดียว
  → 9–13 side / reason / quantity / value / gap จาก decision เดียวกัน
  → 14–17 price-path recurrence จาก anchor
  → 18 validate 17 คอลัมน์ + append transaction (idempotent, stale-anchor guard)
```

Manual Run รายแท็บบังคับลำดับและเปิดเผยคอลัมน์ทีละคอลัมน์ของ row เดียว ส่วน All-in
sidebar ทำ read → compute → append ในคลิกเดียว ทั้งสองทางเรียก engine เดียวกัน
(`lego_one_row.py`) จึงได้ row ผลลัพธ์เดียวกันทุกประการ

## Decision object (Step 8 ครั้งเดียว)

```text
value_now = holdings × price
gap       = FIX_C − value_now
ถ้า DNA signal = 0      → PASS_DNA_ZERO
ไม่งั้นถ้า |gap| ≤ DIFF  → PASS_THRESHOLD
ไม่งั้นถ้า gap > DIFF    → READY_BUY  (side = BUY)
ไม่งั้น (gap < −DIFF)    → READY_SELL (side = SELL)
quantity = round(|gap| / price, decimal_precision)   # PASS = 0
```

Step 9–13 เพียง **เปิดเผย** field จาก decision object เดียวกัน ไม่คำนวณซ้ำ

## Price-path recurrence (Step 14–17)

คอลัมน์ `Rₙ/ΔAₙ/Aₙ/Eₙ` เป็น **price-path recurrence** จาก current quote และ anchor
เพียงแถวเดียว ไม่ใช่ broker execution ledger และไม่สแกนประวัติ fill:

```text
แถวแรก (genesis):  P₀ = Pₙ,  R₀ = ΔA₀ = A₀ = E₀ = 0
แถวถัดไป:
  Rₙ  = FIX_C × ln(Pₙ / P₀)
  ΔAₙ = FIX_C × (Pₙ / Pₙ₋₁ − 1)
  Aₙ  = Aₙ₋₁ + ΔAₙ
  Eₙ  = Aₙ − Rₙ
```

`P₀`, `Pₙ₋₁`, `Aₙ₋₁` มาจาก anchor ที่ persist ไว้ (`webull_lego_state`) ค่าเงินคำนวณ
full precision และ round เป็น 2 ตำแหน่งเฉพาะตอนแสดง/ดาวน์โหลด

## Step 18 — append แบบ transaction

Step 18 ทำ Firestore transaction เดียว:

1. ตรวจว่า anchor ที่ Step 0 อ่านมายังเป็น latest (`anchor.version == state.version`)
   ถ้าไม่ใช่ → **stale anchor** ปฏิเสธแบบ fail-closed และให้เริ่ม Step 0 ใหม่
2. ถ้า `run_id` เดิมถูกบันทึกแล้ว → คืน **idempotent success** ไม่สร้างเอกสารซ้ำ
3. สร้าง `webull_lego_rows/{run_id}` และอัปเดต `webull_lego_state/{chain_key}` พร้อมกัน

`run_id` เป็น deterministic จาก chain, anchor version และ snapshot ที่จับไว้ กด Step 18
ซ้ำใน run เดียวจึงไม่เพิ่มเอกสาร (`N_after − N_before = 1` ต่อ successful run เท่านั้น)

## Order panel — หลัง Step 18 (แนวเดียวกับหน้า Manual)

การส่ง order จริงอยู่ **หลัง Step 18** และแยกจากปุ่มคำนวณเสมอ ใช้ gate เดียวกับหน้า
Manual (`pages/Manual.py`) ที่ยิง order จริงได้:

- เปิดเฉพาะ final row ที่เป็น `READY_BUY`/`READY_SELL` · ใช้ได้ทั้ง **UAT (paper)** และ
  **Production (เงินจริง)**
- gate = ติ๊ก **armed checkbox** + พิมพ์ **confirmation phrase** ให้ตรง
  (`PLACE UAT/PRODUCTION SIDE SYMBOL QTY`) จึงกด Submit ได้ — **Preview เป็น optional**
  ไม่บังคับ
- payload มาจาก final row ที่บันทึกแล้ว (`client_order_id` deterministic จาก `run_id`)
- หลัง Submit ใช้ปุ่ม **Query** ยืนยันว่า Webull รับ order (badge จะโชว์ order_id/สถานะ หรือ
  เหตุผลที่ order ไม่ถูกสร้าง เช่น market ปิด/ไม่มีสิทธิ์เทรด)
- UAT เป็น paper — **holdings จริงไม่ขยับ** และ draft row ใช้ snapshot ตอน Step 0
- ผลถูก redact เขียนลง `webull_lego_order_audit` แยกจากสถานะผลการคำนวณ; `SUBMITTED`/
  `PENDING` ไม่ถูกนับเป็น `FILLED`

## จำง่ายสำหรับมือใหม่

- Tab 0 = อ่าน snapshot จริง 1 ชุด + anchor 1 แถว (ไม่มี trade log)
- Step 1–7 = คัดค่าจาก snapshot
- Step 8–13 = decision object เดียว
- Step 14–17 = price-path recurrence จาก anchor แถวเดียว
- Step 18 = validate + append transaction (idempotent, stale-anchor guard)
- Manual Run = เปิดเผยทีละคอลัมน์ของ row เดียว
- All-in = read → compute → append ในคลิกเดียว ให้ row เดียวกับ Manual
- Order panel = หลัง Step 18, READY_BUY/READY_SELL (UAT/Production), gate = armed + phrase (Preview optional)
