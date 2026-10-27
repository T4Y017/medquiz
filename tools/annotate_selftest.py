"""selftest.json sonuçlarını okunur biçimde yazdırır ve GitHub Actions açıklamaları (annotation) üretir."""
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
for r in d["results"]:
    status = "OK " if r["ok"] else "HATA"
    detail = json.dumps(r.get("info"), ensure_ascii=False) if r["ok"] else r.get("error", "")
    line = f"[{status}] {r['step']} ({r['secs']} sn): {detail}"
    print(line)
    level = "notice" if r["ok"] else ("error" if r.get("required") else "warning")
    msg = line.replace("%", "%25").replace("\r", "").replace("\n", " ")
    print(f"::{level} title=selftest::{msg[:900]}")
if d.get("log_tail"):
    print("---- log ----")
    print(d["log_tail"])
    warn = [ln for ln in d["log_tail"].splitlines() if "WARNING" in ln or "ERROR" in ln][-5:]
    for ln in warn:
        print(f"::warning title=selftest-log::{ln[:900]}")
print("SONUÇ:", "BAŞARILI" if d["ok"] else "BAŞARISIZ")
