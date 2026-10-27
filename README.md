# ⚕ MedQuiz — Kaynaklı Tıbbi Quiz Asistanı

Kendi tıbbi kaynaklarından (PDF, Word, metin dosyaları, web sayfaları) **TUS/USMLE tarzı çoktan seçmeli** ve
**klasik uzun cevaplı** sorular üreten, klasik cevapları yapay zekâ ile puanlayan ve kaynaklara dayanarak
soru-cevap yapan bir Windows masaüstü uygulaması. Arka planda bir **RAG** (Retrieval-Augmented Generation)
sistemi çalışır.

> ⚠️ Eğitim aracıdır. Yapay zekâ hata yapabilir; içerik tıbbi tavsiye değildir.

---

## Özellikler

| | |
|---|---|
| 🩺 **Vaka çalışması** | Bir vakayı başvurudan takibe kadar 5-7 aşamada adım adım çöz: her aşamada ayırıcı tanı, tetkik seçimi, tanı, tedavi kararı ver; anında puan, öğretim notu ve kaynak |
| 🧾 **Vaka özeti** | Kesin tanı, ayırıcı tanı tablosu (lehine/aleyhine), tartışma, akılda kalması gerekenler, sık yapılan hatalar · **PDF olarak kaydet** |
| ✍️ **Kendi vakam** | Gördüğün/okuduğun bir vakayı yaz, uygulama onu kütüphanendeki kaynaklarla aşamalı bir öğrenme vakasına dönüştürsün |
| 💬 **Vakaya soru sor** | Vaka sırasında eğitmene soru sor; vaka bitmeden cevabı söylemez, ipucu verir |
| 🔬 **Paraklinik vaka aşamaları** | Hemogram, biyokimya, kan gazı, EKG, görüntüleme, patoloji sonuçları tablo hâlinde (referans aralığı, ↑/↓ işaretli); görev bu sonuçları yorumlatır |
| 📝 **Vaka sonu testi** | Vaka bittikten sonra 0–10 soruluk TUS tarzı test (yarısı paraklinik yorum); aşama ve test puanı ayrı gösterilir |
| 📝 **Quiz** | Çoktan seçmeli (5 şık), klasik veya karışık · Kolay / Orta / Zor / Klinik vaka · konu odaklı ya da tüm kütüphaneden |
| ♾️ **Sürekli mod** | Sorular bittikçe arka planda yenileri üretilir, daha önce sorulanlar tekrarlanmaz |
| ✍️ **Klasik cevap puanlama** | 0–100 puan, doğru değinilen / eksik / hatalı noktalar, örnek cevap |
| 🔁 **Yanlışları tekrar et** | %60 altı sorular tekrar sorulur; doğru yapınca listeden çıkar |
| 📖 **Kaynak gösterme** | Her sorunun hangi belgeden (ve PDF'te hangi sayfadan) geldiği, kaynak metniyle birlikte |
| 💬 **Sohbet** | Kütüphanedeki kaynaklara dayanan, [K1] [K2] atıflı soru-cevap |
| 📚 **Genişletilebilir kütüphane** | Dosya / klasör / web linki ekle, sürükle-bırak, konu etiketi, sil |
| 🤖 **Çoklu yapay zekâ** | Claude, Gemini ve OpenAI anahtarları aynı anda kayıtlı; sol menüden tek tıkla geçiş; biri kota/bağlantı hatası verirse otomatik olarak diğeri devreye girer; her cevapta hangi modelin yanıtladığı görünür |
| 👨‍👩‍👧 **Aile profilleri** | Her kişi kendi ismini seçer, istatistikler ayrı tutulur |
| 📊 **İstatistik** | Genel başarı, türe ve konuya göre (en zayıf konular üstte) |

Kısayollar: **A–E** veya **1–5** şık seçer, **Enter** sonraki soruya geçer.

---

## 1) Hızlı başlangıç (kaynak koddan çalıştırma)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Python 3.11 veya 3.12 önerilir.

## 2) Ailen için .exe / kurulum dosyası üretme

### Yol A — Kendi Windows bilgisayarında (en kolay)
1. [Python](https://www.python.org/downloads/) kur (kurulumda **"Add python.exe to PATH"** işaretli olsun).
2. İsteğe bağlı: [Inno Setup](https://jrsoftware.org/isdl.php) kur → masaüstü kısayollu tek bir kurulum dosyası üretilir.
3. Proje klasöründe **`build_exe.bat`** dosyasına çift tıkla.

Sonuç:
- `installer_output\MedQuiz_Kurulum.exe` → aileye bunu gönder (yönetici izni istemez).
- `dist\MedQuiz\` → kurulumsuz (portable) sürüm; klasörü zipleyip kopyalayabilirsin.

### Yol B — GitHub üzerinde otomatik (Windows'un yoksa bile)
1. Projeyi bir GitHub deposuna yükle.
2. **Actions → Windows build → Run workflow**.
3. Bitince çıktılardan `MedQuiz_Kurulum.exe` ve `MedQuiz_Portable.zip` indir.

> Not: İmzasız uygulamalarda Windows SmartScreen "Windows bilgisayarınızı korudu" diyebilir →
> **Ek bilgi → Yine de çalıştır**.

## 3) İlk kurulum (uygulama içinde)

1. **Ayarlar** → kullanmak istediğin her sağlayıcı için **Anahtar al ↗** ile API anahtarı oluştur, yapıştır →
   satırdaki **Test** ile doğrula → **Birincil yapay zekâ**yı seç → **Kaydet**.
   - Claude anahtarı: console.anthropic.com · Gemini anahtarı: aistudio.google.com
   - "Birincil yanıt veremezse diğerine geç" açıkken biri kota doldurursa ya da bağlantı koparsa diğeri devralır.
   - Sol menüdeki **Yapay zekâ** kutusundan istediğin an Claude ↔ Gemini geçişi yapabilirsin.

> ℹ️ Claude Pro veya Google AI Pro / Gemini Advanced gibi **sohbet abonelikleri** bu uygulamada kullanılamaz;
> uygulama **API anahtarı** ile çalışır ve API kullanımı aboneliklerden ayrı faturalandırılır. Gemini API'nin
> ücretsiz bir kullanım katmanı olabilir; ancak ücretsiz katmanda gönderilen içeriğin Google tarafından ürün
> geliştirmede kullanılabileceğini şartlardan kontrol et.
2. **Kütüphane** → konu etiketi yaz (örn. *Kardiyoloji*) → dosya/klasör/link ekle.
3. **Quiz** → tür, sayı, zorluk seç → **Başlat**.
4. **Vaka** → konu yaz (örn. *pulmoner emboli*) ya da kendi vakanı yapıştır → seviye seç → **Vakayı oluştur**.
   Yarım kalan vakalar "Vakalarım" listesinde saklanır, kaldığın yerden devam edersin.

> 🔒 Kendi vakanı yazarken ad, TC no, tarih, hastane gibi kimlik bilgilerini yazma; metin yapay zekâ sağlayıcısına gönderilir.

İlk açılışta anlamsal arama modeli (~220 MB) bir kez indirilir; inene kadar anahtar kelime araması kullanılır.

**Maliyet:** Her 5 soruluk paket tek bir API isteğidir. Sonnet sınıfı bir modelle yüzlerce soru genelde
birkaç dolar tutar; daha ucuz kullanım için Ayarlar'dan Haiku seçilebilir. Sağlayıcı panelinden harcama limiti
koymanı öneririm.

---

## Mimari (RAG nasıl çalışıyor?)

```
 PDF / DOCX / TXT / Web
          │  core/ingest.py   → metin çıkarma, temizleme, ~1200 karakterlik örtüşen parçalar
          ▼
   SQLite (parçalar + embedding'ler)          core/db.py
          │
          ▼  core/retrieval.py
   ┌──────────────┬────────────────────────┐
   │ BM25          │ Çok dilli embedding     │   (fastembed, yerel ONNX — veri dışarı çıkmaz)
   │ (Türkçe kök)  │ (anlamsal benzerlik)    │
   └──────┬───────┴───────────┬────────────┘
          └── Reciprocal Rank Fusion ──┘
                     │
                     ▼  core/quiz.py
   Bağlam [K1]..[Kn] + talimat → LLM (Claude/OpenAI) → JSON sorular
                     │
                     ▼
   Doğrulama, şık karıştırma, kaynak eşleme, kayıt → Arayüz (ui/)
```

Önerdiğim makalelerle bağlantısı:
- **Chunking + örtüşme**, komşu parçalarla bağlam genişletme → Contextual Retrieval yazısındaki problem.
- **Hibrit arama (BM25 + dense)** → DPR (dense) + klasik arama; RRF ile birleştirme.
- **Az kullanılan parçalara öncelik** → tüm kütüphanenin zamanla kapsanması (quiz'e özgü retrieval).
- **Şıkların yerel olarak karıştırılması** → LLM'lerin doğru cevabı belli konumlara koyma eğilimine karşı.
- **Klasik cevap puanlama** → RAGAS'taki "LLM-as-judge" fikrinin kullanıcı cevabına uygulanması.

Geliştirme fikirleri: reranker (cross-encoder), HyDE ile sorgu genişletme, görsel/tablo içeren PDF'ler için OCR,
aralıklı tekrar (spaced repetition), soru kalitesini ikinci bir LLM çağrısıyla doğrulama (Self-RAG/CRAG fikri).

## Dosya yapısı

```
app.py                 ana pencere, giriş noktası
core/config.py         ayarlar (%APPDATA%\MedQuiz\settings.json)
core/db.py             SQLite: belgeler, parçalar, embedding'ler, soru geçmişi
core/ingest.py         yükleyiciler + parçalama
core/retrieval.py      BM25 + embedding hibrit arama, quiz bağlamı seçimi
core/llm.py            Claude / Gemini / OpenAI istemcisi, otomatik yedekleme (RoutedLLM), JSON ayrıştırma
core/quiz.py           soru üretimi, puanlama, sohbet istemleri (prompt'lar burada)
core/case.py           aşamalı vaka üretimi, aşama puanlama, Sokratik vaka sohbeti
ui/*.py                PySide6 arayüz sayfaları
medquiz.spec           PyInstaller yapılandırması
build_exe.bat          tek tıkla Windows paketleme
installer.iss          Inno Setup kurulum betiği
```

## Veriler nerede?

`%APPDATA%\MedQuiz\` → `medquiz.db` (kütüphane + geçmiş), `settings.json` (API anahtarı dahil),
`models\` (embedding modeli), `medquiz.log`. Ayarlar → **Veri klasörünü aç**. Yedeklemek için bu klasörü kopyala.

## Büyük kitaplar

- PDF'ler hızlı okunur (pdfium); 1500 sayfalık bir kitap birkaç saniyede eklenir, ilerleme sayfa sayfa gösterilir.
- Anahtar kelime araması hemen hazırdır; anlamsal (embedding) dizin **arka planda** parça parça hazırlanır.
  Bu sürerken quiz, vaka ve sohbet beklemeden çalışır; ilerleme Kütüphane sekmesinin altında görünür.
- İçindekiler, dizin ve kaynakça sayfaları otomatik ayıklanır; konu belirtmeden yapılan quizlerde bu sayfalardan
  soru üretilmez.
- Taranmış (resim) sayfalar algılanır ve uyarı verilir; bu sayfalardan soru üretmek için önce OCR gerekir.

## Sorun bildirme

Ayarlar → **Tanılama raporunu kopyala** → sohbete yapıştır. Rapor sürüm, ayarlar, kütüphane durumu ve son log
satırlarını içerir; API anahtarları otomatik olarak çıkarılır.

## Otomatik test (geliştirici)

Her sürüm etiketinde (`v*`) GitHub Actions, paketlenmiş exe'yi gerçek bir Windows makinesinde
`MedQuiz.exe --selftest` ile sınar: PDF okuma, arama, anlamsal model, arayüz ve PDF dışa aktarma.
Depo ayarlarında **Secrets → Actions** altına `GEMINI_API_KEY` (ve/veya `ANTHROPIC_API_KEY`) eklenirse
gerçek yapay zekâ ile quiz, puanlama ve vaka üretimi de test edilir. Anahtar loglarda görünmez.

## Sorun giderme

| Sorun | Çözüm |
|---|---|
| "PDF'ten metin çıkarılamadı" | Taranmış PDF; önce OCR uygula (örn. Adobe, ocrmypdf) |
| Quiz/vaka "kullanılabilir soru çıkmadı" diyor | Tekrar dene; sürerse Ayarlar'dan modeli değiştir, konu alanına daha belirgin bir konu yaz |
| Gemini "kullanım sınırına ulaşıldı" | Ücretsiz katmanın dakikalık/günlük sınırı; bir dakika bekle |
| "API anahtarı geçersiz" | Anahtarı tekrar kopyala, başında/sonunda boşluk olmasın |
| "Seçilen model bulunamadı" | Ayarlar'da listeden başka model seç ya da güncel model adını yaz |
| Web sayfası eklenmiyor | Giriş gerektiren / JavaScript ile yüklenen sayfa; metni kopyalayıp .txt olarak ekle |
| Anlamsal arama kapalı görünüyor | İnternet yoksa model inmemiştir; anahtar kelime araması yine çalışır |
| Hata ayrıntısı | `%APPDATA%\MedQuiz\medquiz.log` |
