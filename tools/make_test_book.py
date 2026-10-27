"""CI için sentetik bir 'kitap' PDF'i üretir (içindekiler, bölümler, kaynakça dahil)."""
import glob
import os
import random
import sys

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

out, n = sys.argv[1], int(sys.argv[2])
font = "Helvetica"
for cand in [r"C:\Windows\Fonts\arial.ttf", *glob.glob("/usr/share/fonts/**/DejaVuSans.ttf", recursive=True)]:
    if os.path.exists(cand):
        pdfmetrics.registerFont(TTFont("TR", cand))
        font = "TR"
        break
random.seed(1)
topics = ["Diyabetik ketoasidoz", "Akut miyokard enfarktüsü", "Pulmoner emboli", "Akut pankreatit", "Sepsis",
          "Hipertansiyon", "Kronik böbrek hastalığı", "Astım", "KOAH", "Anemi", "Hipotiroidi", "Siroz"]
facts = {
    "Diyabetik ketoasidoz": ["Mutlak insülin eksikliği lipolizi ve keton üretimini artırır.",
                             "Tanı: glukoz > 250 mg/dL, pH < 7.30, HCO3 < 18 mEq/L ve ketonemi.",
                             "Potasyum 3.3 mEq/L altındaysa insülin başlanmadan önce potasyum replasmanı yapılır.",
                             "Kussmaul solunumu metabolik asidoza solunumsal kompanzasyondur.",
                             "Anyon açığı = Na − (Cl + HCO3); DKA'da artmıştır."],
}
generic = ["{t} kliniğinde en sık bulgu ağrı ve halsizliktir.", "Tanıda laboratuvar bulguları önemlidir.",
           "Tedavide ilk basamak destek tedavisidir.", "Ayırıcı tanıda benzer tablolar dışlanmalıdır.",
           "Görüntülemede ultrasonografi ve bilgisayarlı tomografi kullanılır."]
c = canvas.Canvas(out, pagesize=A4)


def page(lines):
    c.setFont(font, 9)
    y = 800
    for ln in lines:
        c.drawString(40, y, ln[:110])
        y -= 12
        if y < 40:
            break
    c.showPage()


for _ in range(5):
    page([f"{random.choice(topics)} {'.' * 60} {random.randint(1, n)}" for _ in range(60)])
for p in range(n - 15):
    t = topics[(p // 30) % len(topics)]
    pool = facts.get(t, []) + [g.format(t=t) for g in generic]
    page(([f"BÖLÜM {(p // 30) + 1}: {t.upper()}"] if p % 30 == 0 else []) + [random.choice(pool) for _ in range(55)])
for _ in range(10):
    page([f"{random.randint(1, 999)}. Smith J, et al. J Med. {random.randint(1990, 2024)};12:{random.randint(1, 99)}."
          for _ in range(60)])
c.save()
print(out, n, "pages")
