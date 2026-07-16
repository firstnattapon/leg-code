# Webull Dashboard

โปรเจกต์มี Dashboard เดิม, Manual Test Lab และแอป `lego_dashboard.py` สำหรับเรียนรู้
Shannon Demon แบบ 19 แท็บ

## One-New-Row LEGO Chain

LEGO Chain รุ่นนี้ไม่โหลด `shannon_demon_trades` หลายแถวมาต่อคอลัมน์อีกแล้ว:

```text
Step 0  Webull Account/Balance/Positions/Quote snapshot + latest final anchor
Step 1–17  คำนวณ draft row เดียวทีละคอลัมน์
Step 18  validate 17 columns + append Firestore transaction
หลัง Step 18  UAT Preview/Submit จาก immutable final row
```

- หนึ่ง successful run เพิ่ม `webull_lego_rows` exactly 1 document
- Step 0 อ่านเฉพาะ `webull_lego_state/{chain_key}` และ latest row ที่ pointer ชี้
- `run_id` เป็น deterministic document id; retry จึงไม่สร้าง row ซ้ำ
- stale anchor ถูกปฏิเสธและต้องเริ่ม Step 0 ใหม่
- Manual และ All-in ใช้ calculation/persistence contract เดียวกัน
- Production อ่าน snapshot และคำนวณได้ แต่เป็น read-only เสมอ

## Run

```powershell
python -m pip install -r requirements-dev.txt
python -m streamlit run lego_dashboard.py
```

1. Tab 0: เลือก environment แล้วกรอก credentials, Symbol, DNA_CODE, FIX_C และ DIFF
2. กด `Connect & Create New Draft Row`
3. รัน Step 1–17 ทีละแท็บ หรือใช้ `Run ALL 0 → 18 (NEW ROW)`
4. Manual flow: กด `Finalize Step 18 + Append New Row`
5. เฉพาะ `READY_BUY/READY_SELL` ใน Test (UAT): Preview, พิมพ์ confirmation phrase และ Submit

ไฟล์ [webull_lego_single_file.py](webull_lego_single_file.py) รัน contract เดียวกันโดย
ไม่ import โมดูลในโปรเจกต์และไม่มี order mutation:

```powershell
$env:WEBULL_ACCOUNT_ID="..."
$env:WEBULL_APP_KEY="..."
$env:WEBULL_APP_SECRET="..."
$env:GOOGLE_APPLICATION_CREDENTIALS="C:\safe\firebase.json"
python webull_lego_single_file.py --environment "Test (UAT)" --symbol AAPL --dna-code "bypass:100"
```

เพิ่ม `--persist` เมื่อต้องการ append final row ด้วย transaction

## Calculation Contract

แถวแรก:

```text
DNA step = 0
P₀ = Pₙ
R₀ = ΔA₀ = A₀ = E₀ = 0
```

แถวถัดไปใช้ latest final anchor:

```text
Rₙ  = FIX_C × ln(Pₙ/P₀)
ΔAₙ = FIX_C × (Pₙ/Pₙ₋₁ − 1)
Aₙ  = Aₙ₋₁ + ΔAₙ
Eₙ  = Aₙ − Rₙ
```

Decision:

```text
gap = FIX_C − holdings × price
DNA signal = 0       → PASS_DNA_ZERO
|gap| ≤ DIFF         → PASS_THRESHOLD
gap > DIFF           → READY_BUY
gap < −DIFF          → READY_SELL
quantity = round(|gap| / price, decimal_precision)
```

ค่าคำนวณใช้ full precision; final presentation/export round money columns เป็น 2 ตำแหน่ง

## Firestore

```toml
[lego_dashboard]
rows_collection = "webull_lego_rows"
state_collection = "webull_lego_state"
order_audit_collection = "webull_lego_order_audit"
fix_c = 1500.0
diff = 30.0
decimal_precision = 5
audit_to_firestore = true
```

Service account ต้องอ่าน/เขียน rows และ state collections เพื่อใช้ Step 18 transaction
ส่วน credentials และ raw sensitive responses อยู่ใน session เท่านั้น

## Security

- Default environment คือ Test (UAT)
- Production ไม่มี Preview/Submit path
- order panel ปรากฏหลัง Step 18 persisted เท่านั้น
- PASS และ unpersisted draft ส่ง order ไม่ได้
- UAT ต้อง Preview payload เดิมและพิมพ์ confirmation phrase ให้ตรง
- ห้าม commit `.streamlit/secrets.toml`, `.env` หรือ credentials

## Planning Artifacts

- `webull_dashboard_overhaul_five_step_prompt.json` — implementation prompt หลัก
- `webull_lego_chain_plan.json` — machine-readable summary
- `webull_lego_chain_guide.html` — offline overview

## Test

```powershell
python -m pytest -q
```
