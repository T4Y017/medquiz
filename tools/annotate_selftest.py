"""selftest.json sonuçlarını okunur biçimde yazdırır ve GitHub Actions açıklamaları (annotation) üretir."""
import json
import sys

# Windows konsolu (cp1252) Türkçe karakterlerde çökmesin
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:  # noqa: BLE001
    print(f"::error title=selftest::selftest.json okunamadı (uygulama çökmüş olabilir): {e}")
    sys.exit(1)
if len(sys.argv) > 2:
    print(f"::notice title=selftest-exit::Uygulama çıkış kodu: {sys.argv[2]}")
core, ai = [], []
for r in d["results"]:
    status = "OK " if r["ok"] else "HATA"
    detail = json.dumps(r.get("info"), ensure_ascii=False) if r["ok"] else r.get("error", "")
    line = f"[{status}] {r['step']} ({r['secs']} sn): {detail}"
    print(line)
    (ai if ":" in r["step"].split(" ")[0] else core).append((r, line))


def esc(x):
    return x.replace("%", "%25").replace("\r", "").replace("\n", " ")


# GitHub adım başına en fazla 10 notice/10 warning gösterir: temel adımlar tek satırda
core_ok = all(r["ok"] for r, _ in core if r.get("required"))
summary = " | ".join(f"{'OK' if r['ok'] else 'HATA'} {r['step']} {r['secs']}sn" for r, _ in core)
print(f"::{'notice' if core_ok else 'error'} title=selftest-temel::{esc(summary)[:900]}")
for r, line in core:
    if not r["ok"]:
        print(f"::{'error' if r.get('required') else 'warning'} title=selftest::{esc(line)[:900]}")
for r, line in ai:
    print(f"::{'notice' if r['ok'] else 'warning'} title=selftest-ai::{esc(line)[:900]}")
if d.get("log_tail"):
    print("---- log ----")
    print(d["log_tail"])
    warn = [ln for ln in d["log_tail"].splitlines() if "WARNING" in ln or "ERROR" in ln][-3:]
    for ln in warn:
        print(f"::warning title=selftest-log::{esc(ln)[:600]}")
print("SONUÇ:", "BAŞARILI" if d["ok"] else "BAŞARISIZ")
