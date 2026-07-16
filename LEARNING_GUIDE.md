# Learning Guide — Webull LEGO One-New-Row 0→18

## แนวคิด

หนึ่งรอบการทำงานสร้างข้อมูลใหม่หนึ่งแถว ไม่คัดลอก trade history:

```text
current Webull snapshot
  + latest finalized anchor
  → 17 calculated columns
  → Step 18 transaction
  → one new Firestore document
```

Snapshot ของหนึ่ง run เป็น immutable: ราคาและ holdings จะไม่ refresh ระหว่างแท็บ
เพื่อไม่ให้คอลัมน์ใน row เดียวกันอ้างคนละเวลา

## Step 0

อ่าน API จริง:

1. Account list
2. Account balance
3. Account positions
4. Market snapshot ของ Symbol ที่ผู้ใช้กรอก

Firestore อ่านเพียง:

1. `webull_lego_state/{chain_key}`
2. latest row ที่ pointer ชี้ใน `webull_lego_rows`

`old_trade_log_reads = 0`; ไม่มี query ไป `shannon_demon_trades`

`chain_key` แยกด้วย environment, account fingerprint, symbol และ hash ของ
FIX_C/DIFF/DNA/precision การเปลี่ยน strategy parameter จึงเริ่ม chain ใหม่

## Steps 1–17

| Step | ค่าที่สร้าง |
|---:|---|
| 1 | เวลา UTC จาก snapshot |
| 2 | Symbol ปัจจุบัน |
| 3 | `SNAPSHOT_READY` ระหว่าง draft |
| 4 | previous DNA step + 1 หรือ 0 |
| 5 | deterministic `DNA_CODE[step]` |
| 6 | positive snapshot quote |
| 7 | Webull-observed holdings |
| 8–13 | action, side, reason, quantity, value และ target gap |
| 14–17 | Rₙ, ΔAₙ, Aₙ และ Eₙ จาก latest anchor |

Step 18 เปลี่ยน status เป็น `PASS_DNA_ZERO`, `PASS_THRESHOLD`, `READY_BUY`
หรือ `READY_SELL`

## Recurrence

แถวแรกใช้ `P₀=Pₙ` และค่า ledger เป็นศูนย์ จากนั้น:

```text
Rₙ  = FIX_C × ln(Pₙ/P₀)
ΔAₙ = FIX_C × (Pₙ/Pₙ₋₁ − 1)
Aₙ  = Aₙ₋₁ + ΔAₙ
Eₙ  = Aₙ − Rₙ
```

ค่า full precision อยู่ใน document metadata ส่วน final table round เงินเป็น 2 ตำแหน่ง

## Step 18 Transaction

Step 18 ตรวจ:

- exact 17-column order
- exactly one row
- stage hashes
- `chain_key`, `run_id` และ anchor version

จากนั้น transaction จะ:

1. ตรวจว่า `run_id` ยังไม่มี หรือคืน idempotent success ถ้ามี document เดิม
2. ตรวจ latest pointer/version ว่ายังตรงกับ Step 0
3. create `webull_lego_rows/{run_id}`
4. update `webull_lego_state/{chain_key}`

ถ้า pointer เปลี่ยนจะเกิด stale-anchor error และต้องเริ่ม Step 0 ใหม่

## Order Safety

- calculation และ All-in ไม่ส่ง order อัตโนมัติ
- order panel แสดงหลัง final row persisted แล้วเท่านั้น
- `PASS_*` ไม่มี order panel
- Test (UAT) ต้อง Preview payload เดิมและพิมพ์ confirmation phrase
- Production เป็น read-only ไม่มี Submit button
- acknowledgement แบบ PENDING/SUBMITTED ไม่ถูกเรียกว่า FILLED
- order audit ถูก redact และเก็บใน `webull_lego_order_audit` หรือ session fallback

## Single File

```powershell
python webull_lego_single_file.py --environment "Test (UAT)" --symbol AAPL --dna-code "bypass:100"
```

ค่าเริ่มต้นคำนวณ/ส่งออกโดยไม่ persist เพิ่ม `--persist` เพื่อใช้ Step 18 transaction
