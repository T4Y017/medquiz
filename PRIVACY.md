# MedQuiz — Gizlilik Politikası / Privacy Policy

_Son güncelleme / Last updated: 29.10.2026_

## Türkçe

MedQuiz, bilgisayarınızda çalışan bir masaüstü çalışma uygulamasıdır. Geliştiricinin herhangi bir sunucusu yoktur;
verileriniz geliştiriciye gönderilmez.

**Google ile giriş yaparsanız** MedQuiz yalnızca şu izinleri ister:
- `openid`, `email`: hangi Google hesabıyla giriş yaptığınızı göstermek için e-posta adresiniz.
- `drive.appdata`: Google Drive'ınızda **yalnızca MedQuiz'e ait gizli uygulama alanına** (appDataFolder) yazıp okumak.
  MedQuiz Drive'ınızdaki diğer dosyaları göremez, değiştiremez.

Bu alana yazılanlar: uygulama ayarlarınız (girdiğiniz yapay zekâ API anahtarları dahil), quiz cevaplarınız ve
istatistikleriniz, kelime havuzunuz, özetleriniz, sohbetleriniz, vakalarınız ve kütüphanenize eklediğiniz belgeler.
Bu veriler şifrelenerek saklanır ve yalnızca sizin Google hesabınızla giriş yaptığınız MedQuiz kurulumları
tarafından okunur. Hiçbir veri üçüncü kişilerle paylaşılmaz, satılmaz ya da reklam için kullanılmaz.

Soru üretimi, özet ve sohbet için metinler, sizin seçtiğiniz yapay zekâ sağlayıcısına (Anthropic, Google Gemini ya da
OpenAI) **kendi API anahtarınızla** doğrudan gönderilir; bu sağlayıcıların kendi gizlilik koşulları geçerlidir.

Erişimi istediğiniz zaman kaldırabilirsiniz: MedQuiz'de Ayarlar → Hesap → **Çıkış yap**, ya da
https://myaccount.google.com/permissions adresinden MedQuiz'in erişimini kaldırın. Drive'daki uygulama verisini
silmek için Google Drive → Ayarlar → Uygulamaları yönet → MedQuiz → "Gizli uygulama verilerini sil".

İletişim: https://github.com/T4Y017/medquiz/issues

## English

MedQuiz is a desktop study app that runs on your computer. The developer operates no servers and receives none of
your data.

If you **sign in with Google**, MedQuiz requests only `openid`, `email` (to show which account is connected) and
`drive.appdata` (to read and write **only MedQuiz's own hidden app-data folder** in your Google Drive; it cannot see
or modify any other Drive files). Stored there, encrypted: your app settings (including AI API keys you entered),
quiz answers and statistics, vocabulary, summaries, chats, cases and library documents. This data is used only to
sync your MedQuiz installations, is never shared with third parties, sold, or used for advertising.

Text for question generation, summaries and chat is sent directly to the AI provider you choose (Anthropic, Google
Gemini or OpenAI) using your own API key, under that provider's terms.

You can revoke access at any time (MedQuiz → Settings → Account → Sign out, or https://myaccount.google.com/permissions)
and delete the app data via Google Drive → Settings → Manage apps → MedQuiz → "Delete hidden app data".

Contact: https://github.com/T4Y017/medquiz/issues
