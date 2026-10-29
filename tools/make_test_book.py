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


def table_page(intro, caption, headers, rows, widths):
    """Izgara çizgili gerçek bir tablo (özetteki tablo çıkarma testini besler)."""
    c.setFont(font, 10)
    y = 800
    for ln in intro:
        c.drawString(40, y, ln)
        y -= 14
    y -= 10
    c.setFont(font, 10)
    c.drawString(40, y, caption)
    y -= 8
    rh = 18
    allr = [headers] + rows
    x0, total = 40, sum(widths)
    for ri, r in enumerate(allr):
        top = y - ri * rh
        c.line(x0, top, x0 + total, top)
        x = x0
        for ci, cell in enumerate(r):
            c.setFont(font, 9)
            c.drawString(x + 4, top - 13, str(cell))
            x += widths[ci]
    bottom = y - len(allr) * rh
    c.line(x0, bottom, x0 + total, bottom)
    x = x0
    for w in [0] + widths:
        x += w
        c.line(x, y, x, bottom)
    c.drawString(40, bottom - 20, "Tablo verileri kaynak kılavuzdan özetlenmiştir.")
    c.showPage()


for _ in range(5):
    page([f"{random.choice(topics)} {'.' * 60} {random.randint(1, n)}" for _ in range(60)])
table_page(["BÖLÜM 0: DİYABETİK KETOASİDOZ TEDAVİSİ",
            "Diyabetik ketoasidoz mutlak insülin eksikliği ile gelişen acil bir durumdur.",
            "Tedavinin temeli sıvı, insülin ve elektrolit replasmanıdır (Tablo 1)."],
           "Tablo 1. Diyabetik ketoasidozda başlangıç tedavisi",
           ["Basamak", "Uygulama", "Doz / hedef"],
           [["Sıvı", "%0,9 NaCl", "1000-1500 mL ilk saat"], ["İnsülin", "Kristalize insülin infüzyonu", "0,1 U/kg/saat"],
            ["Potasyum", "K < 3,3 mEq/L ise önce replasman", "20-30 mEq/L sıvıya"],
            ["Bikarbonat", "Yalnızca pH < 6,9 ise", "100 mmol 2 saatte"],
            ["İzlem", "Glukoz, elektrolit, kan gazı", "1-2 saatte bir"]], [90, 230, 170])
table_page(["Ketoasidozun şiddeti laboratuvar değerlerine göre sınıflandırılır (Tablo 2)."],
           "Tablo 2. Diyabetik ketoasidoz şiddet sınıflaması",
           ["Ölçüt", "Hafif", "Orta", "Ağır"],
           [["Arteriyel pH", "7,25-7,30", "7,00-7,24", "< 7,00"], ["HCO3 (mEq/L)", "15-18", "10-14", "< 10"],
            ["Anyon açığı", "> 10", "> 12", "> 12"], ["Bilinç", "Uyanık", "Uyanık/uykulu", "Stupor/koma"]],
           [120, 110, 110, 110])
for p in range(n - 17):
    t = topics[(p // 30) % len(topics)]
    pool = facts.get(t, []) + [g.format(t=t) for g in generic]
    page(([f"BÖLÜM {(p // 30) + 1}: {t.upper()}"] if p % 30 == 0 else []) + [random.choice(pool) for _ in range(55)])
for _ in range(10):
    page([f"{random.randint(1, 999)}. Smith J, et al. J Med. {random.randint(1990, 2024)};12:{random.randint(1, 99)}."
          for _ in range(60)])
c.save()
print(out, n, "pages")
