# Learning Guide — Webull LEGO 0→18 ฉบับใช้งานจริง

อ่านหน้านี้ก่อน 5 นาที แล้วเริ่มได้จาก `lego_dashboard.py` หรือไฟล์เดียว
`webull_lego_single_file.py`

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
2. ใส่ Account ID, App Key, App Secret, symbol และ DNA_CODE
3. กด `Connect & Load` เพื่ออ่าน API จริง หรือกด `Run ALL 0 → 18 (REAL READ)`
   ที่ sidebar หลังเชื่อมต่อแล้ว

### Single File

```powershell
python -m pip install pandas numpy google-cloud-firestore google-auth webull-openapi-python-sdk
$env:WEBULL_ACCOUNT_ID="..."
$env:WEBULL_APP_KEY="..."
$env:WEBULL_APP_SECRET="..."
$env:GOOGLE_APPLICATION_CREDENTIALS="C:\safe\firebase.json"
python webull_lego_single_file.py --environment "Test (UAT)" --symbol AAPL --dna-code "bypass:100"
```

เปลี่ยนเป็น `--environment Production` เมื่อต้องการอ่านบัญชี Production จริง
ไฟล์ All-in ไม่มีเมธอด place/cancel จึงเป็น read-only ทั้งสอง environment

## Step 0 อ่านอะไรจริง

แอปใช้ official Webull Python SDK และ endpoint ตาม environment:

| Environment | Endpoint |
|---|---|
| Test (UAT) | `th-api.uat.webullbroker.com` |
| Production | `api.webull.co.th` |

Step 0 เรียก idempotent read endpoints เหล่านี้จริง:

1. Account list
2. Account balance
3. Account positions
4. Market snapshot เมื่อมี symbol
5. Firestore trade collection ตามค่า `[lego_dashboard]`

read calls retry ได้สูงสุด 3 ครั้งเมื่อเป็น network/429/5xx แต่ authentication หรือ
validation error จะหยุดทันที Credentials อยู่เฉพาะ session memory และไม่ถูกเขียนลง
DataFrame, JSON download หรือ audit

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

### กฎ provenance

`dna_signal` ที่บอทบันทึกใน Firestore มีสิทธิ์ก่อนเสมอ Decoder เติมเฉพาะแถวที่
signal ว่างและมี `dna_step` เป็นจำนวนเต็มไม่ติดลบภายในช่วง sequence เท่านั้น
จึงไม่แก้ประวัติจริงและไม่สร้าง step ขึ้นมาเอง

## LEGO chain ทำงานอย่างไร

```text
0 real Webull + Firestore reads
  → 1 เวลา UTC
  → 2 สินทรัพย์
  → 3 สถานะ
  → 4 DNA step
  → 5 DNA signal (logged first, decoder fallback)
  → 6 ราคา quote
  → 7 holdings ที่ Webull ยืนยัน
  → 8–13 decision fields + FIX_C gap
  → 14–17 broker-confirmed execution ledger
  → 18 Final DataFrame + separate what-if
```

การกด Run รายแท็บยังบังคับลำดับและเหมาะกับการเรียนทีละบล็อก ส่วน All-in sidebar
เรียก Step 0 ใหม่จริง แล้วรัน 1→18 ใน loop เดียว ผลลัพธ์ของทั้งสองทางใช้ contract
17 คอลัมน์เดียวกัน

## เงินจริงกับ what-if ห้ามปะปน

Broker-confirmed ledger ต้องมีครบทุกข้อ:

- terminal fill status
- cumulative filled quantity มากกว่า 0
- execution price จริง (ห้ามใช้ quote แทน)
- `position_reconciled is True`
- order ID เพื่อ deduplicate cumulative partial fills
- fee จะถูกหักเมื่อ log มี cumulative fee

เมื่อหลักฐานไม่ครบ ค่า `Rₙ/ΔAₙ/Aₙ/Eₙ` หลักจะว่าง การว่างเป็นผลที่ถูกต้อง

What-if เป็นตารางแยก ใช้ positive quote ทุกจุด:

```text
ΔAₙ = FIX_C × (Pₙ/Pₙ₋₁ − 1)
Aₙ  = ΣΔAₙ
Rₙ  = FIX_C × ln(Pₙ/P₀)
Eₙ  = Aₙ − Rₙ
```

## เหตุผลที่ All-in ไม่ส่ง order

read-only loop ทำซ้ำได้โดยไม่สร้าง side effect จึงเหมาะกับ Test และ Production
การส่ง order จริงจะแยกออกมาที่ **order panel** ซึ่งอยู่ในทุกแท็บ 0–18 และแยกจากปุ่ม Run
เสมอ ต้อง Preview payload เดิมก่อน, พิมพ์ confirmation phrase ให้ตรง และ (Production)
เปิด safety switch + ทวน account/symbol/side/quantity ก่อนยิง `place_order` จริง จากนั้น
ใช้ Query อ่านสถานะ · `SUBMITTED`/`PENDING` ไม่ถูกนับเป็น `FILLED` · การวิเคราะห์ข้อมูล
(All-in/Run) จะไม่แอบเปลี่ยนบัญชี

## จำง่ายสำหรับมือใหม่

- Tab 0 = อ่านของจริง
- Step 1–13 = จัดข้อมูลและอธิบาย decision
- Step 14–17 = เชื่อเฉพาะ fill ที่พิสูจน์ได้
- Step 18 = export
- Manual Run = เรียนทีละ LEGO
- All-in = ยิง API ใหม่และต่อครบทุก LEGO (read-only)
- Order panel = มีทุกแท็บ ส่ง order จริงเมื่อกด Submit เอง
- Production = ส่ง order เงินจริงได้เฉพาะเมื่อเปิด safety switch + confirmation phrase
