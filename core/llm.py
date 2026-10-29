"""Claude / Gemini / OpenAI için ortak istemci arayüzü ve sağlayıcılar arası otomatik yedekleme."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterator

from .config import PROVIDERS, Settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, msg: str, fallback_ok: bool = False, transient: bool = False, provider_wide: bool = False,
                 cooldown: float = 0.0, retry_same: bool = True, retry_after: float = 0.0):
        super().__init__(msg)
        self.retry_after = retry_after  # sağlayıcının önerdiği bekleme (sn); 0 = bilinmiyor
        self.cooldown = cooldown        # bu model kaç saniye dinlenmeye alınsın
        self.retry_same = retry_same    # aynı modeli hemen bir kez daha denemek mantıklı mı
        self.provider_wide = provider_wide  # anahtar/kota/izin: aynı sağlayıcının diğer modelleri de başarısız olur
        self.fallback_ok = fallback_ok  # başka bir model/sağlayıcıyla denemek mantıklı mı?
        self.transient = transient      # geçici (yoğunluk, hız sınırı, zaman aşımı): kısa bekleyip tekrar denenebilir


def _classify(e: Exception, provider: str) -> LLMError:
    msg = str(e)
    low = msg.lower()
    name = PROVIDERS.get(provider, {}).get("short", provider)
    status = getattr(e, "status_code", None)
    if status == 401 or "authentication" in low or "invalid x-api-key" in low or "incorrect api key" in low \
            or "api key not valid" in low or "api_key_invalid" in low:
        return LLMError(f"{name} API anahtarı geçersiz. Ayarlar sekmesinden kontrol edin.", True, provider_wide=True)
    if "credit" in low or "billing" in low or "quota" in low or "insufficient" in low or "resource_exhausted" in low:
        if provider == "gemini":
            return LLMError("Gemini kullanım sınırına ulaşıldı (ücretsiz katmanda dakikalık ve günlük istek sınırı "
                            "vardır). Bir dakika bekleyip tekrar deneyin; sürekli oluyorsa günlük sınır dolmuştur.", True,
                            transient=True, cooldown=180, retry_same=False)
        return LLMError(f"{name} hesabında bakiye/kota yok ya da günlük kota doldu.", True, provider_wide=True)
    if status == 429 or ("rate" in low and "limit" in low):
        return LLMError(f"{name}: çok sık istek gönderildi (hız sınırı). Biraz bekleyip tekrar deneyin.", True, True,
                        cooldown=90)
    if status == 404 or "not_found" in low or ("model" in low and "not" in low and "found" in low):
        return LLMError(f"{name}: seçilen model bulunamadı. Ayarlar'dan başka bir model seçin.", True)
    if "connection" in low or "timed out" in low or "timeout" in low:
        if "timed out" in low or "timeout" in low:
            return LLMError(f"{name} zamanında yanıt vermedi (sunucu yoğun olabilir).", True, True,
                            cooldown=120, retry_same=False)
        return LLMError(f"{name} sunucusuna bağlanılamadı. İnternet bağlantınızı kontrol edin.", True, True)
    if status and status >= 500 or "overloaded" in low or "unavailable" in low:
        return LLMError(f"{name} sunucusu şu an yanıt veremiyor (yoğunluk). Biraz sonra tekrar deneyin.", True, True,
                        cooldown=60)
    if status == 403 or "permission" in low or "location is not supported" in low:
        return LLMError(f"{name}: bu anahtarın erişim izni yok ya da bölge desteklenmiyor.", True, provider_wide=True)
    return LLMError(f"{name} hatası: {msg[:300]}", False)


def _from_gemini(e, model: str) -> LLMError:
    """Gemini yerel API hatasını, kota türü ve önerilen bekleme süresiyle birlikte LLMError'a çevirir."""
    st, msg, low = e.status, e.message, e.message.lower()
    if e.kind == "timeout":
        return LLMError(f"Gemini ({model}) zamanında yanıt vermedi.", True, True, cooldown=60, retry_same=True,
                        retry_after=3)
    if e.kind == "network":
        return LLMError(f"Gemini sunucusuna bağlanılamadı: {msg}", True, True, retry_after=3)
    if e.kind == "blocked":
        return LLMError(f"Gemini yanıtı güvenlik filtresine takıldı.", True)
    if e.kind in ("length", "empty"):
        return LLMError(f"Gemini ({model}): {msg}", True, True, retry_after=1)
    if st in (400, 401) and ("api key" in low or "api_key" in low):
        return LLMError("Gemini API anahtarı geçersiz. Ayarlar sekmesinden kontrol edin.", True, provider_wide=True)
    if st == 403:
        return LLMError(f"Gemini: bu anahtarın erişim izni yok ya da bölge desteklenmiyor ({msg[:120]}).", True,
                        provider_wide=True)
    if st == 404:
        return LLMError(f"Gemini: '{model}' modeli bulunamadı. Ayarlar'dan başka bir model seçin.", True)
    if st == 429:
        if e.daily_quota:
            from . import usage
            usage.note_limit(model, e.quota_value)
            lim = f" (günlük sınır: {e.quota_value} istek)" if e.quota_value else ""
            return LLMError(f"{model} için ücretsiz günlük kota doldu{lim}. Kota her gün Türkiye saatiyle "
                            "10:00-11:00 arasında sıfırlanır.", True, cooldown=3 * 3600, retry_same=False)
        wait = e.retry_after or 15
        return LLMError(f"{model} dakikalık istek sınırına ulaştı; {int(wait)} sn sonra tekrar denenecek.", True, True,
                        cooldown=wait, retry_after=wait)
    if st >= 500:
        return LLMError(f"{model} şu an yoğun (Google sunucusu {st}).", True, True, cooldown=45,
                        retry_after=e.retry_after or 8)
    return LLMError(f"Gemini hatası ({st}): {msg[:300]}", False)


class LLMClient:
    """Tek bir sağlayıcıya bağlı istemci."""

    # Sağlayıcının reddettiği isteğe bağlı parametreler (provider, model, param) — bir kez öğrenilir.
    _unsupported: set[tuple[str, str, str]] = set()
    THINKING_HEADROOM = 8192   # Gemini 3 "düşünme" token'ları çıktı bütçesinden yer; JSON yarım kalmasın

    def __init__(self, settings: Settings, provider: str | None = None, model: str | None = None):
        self.provider = provider or settings.provider
        if self.provider not in PROVIDERS:
            raise LLMError(f"Bilinmeyen sağlayıcı: {self.provider}")
        self.model = model or settings.model_for(self.provider)
        self.label = f"{PROVIDERS[self.provider]['short']} · {self.model}"
        self.last_finish = ""
        key = settings.key_for(self.provider)
        if not key:
            raise LLMError(f"{PROVIDERS[self.provider]['short']} API anahtarı girilmemiş. Ayarlar sekmesinden ekleyin.",
                           True)
        self.last_usage: dict = {}
        self._native = None
        if self.provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic(api_key=key, timeout=self._timeout(), max_retries=1)
        elif self.provider == "gemini" and settings.extra.get("gemini_api", "native") == "native":
            from .gemini_native import GeminiNative

            self._native = GeminiNative(key, self.model)
        else:
            import openai

            base_url = PROVIDERS[self.provider].get("base_url")
            self._client = openai.OpenAI(api_key=key, base_url=base_url, timeout=self._timeout(), max_retries=1)

    # ------------------------------------------------------------ complete
    @staticmethod
    def _timeout() -> float:
        # Düz sayı: SDK'ların farklı httpx sürümleriyle (httpx / httpx2) uyumlu. Yanıt en fazla 120 sn.
        return 120.0

    def _openai_kwargs(self, max_tokens: int, temperature: float) -> dict:
        kw: dict = {"model": self.model, "temperature": temperature, "max_tokens": max_tokens}
        if self.provider == "gemini":
            kw["max_tokens"] = min(65536, max_tokens + self.THINKING_HEADROOM)
            if (self.provider, self.model, "reasoning_effort") not in self._unsupported:
                kw["reasoning_effort"] = "low"     # daha hızlı, daha az token; soru yazmak için yeterli
        return kw

    def _openai_create(self, kw: dict):
        try:
            return self._client.chat.completions.create(**kw)
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if getattr(e, "status_code", None) == 400 and "reasoning_effort" in kw and (
                    "reasoning" in msg or "thinking" in msg or "unknown" in msg or "unsupported" in msg):
                log.info("%s/%s reasoning_effort desteklemiyor; parametresiz tekrar deneniyor", self.provider, self.model)
                self._unsupported.add((self.provider, self.model, "reasoning_effort"))
                kw = {k: v for k, v in kw.items() if k != "reasoning_effort"}
                return self._client.chat.completions.create(**kw)
            raise

    def _conv(self, messages: list[dict]) -> list[dict]:
        """Çok biçimli içerikleri (PDF) sağlayıcının biçimine çevirir; düz metin mesajlara dokunmaz."""
        import base64
        out = []
        for m in messages:
            c = m["content"]
            if not isinstance(c, list):
                out.append(m)
                continue
            parts = []
            for p in c:
                if p.get("type") == "pdf":
                    b64 = base64.b64encode(p["data"]).decode()
                    if self.provider == "anthropic":
                        parts.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                                       "data": b64}})
                    else:
                        parts.append({"type": "file", "file": {"filename": p.get("name", "kaynak.pdf"),
                                                               "file_data": f"data:application/pdf;base64,{b64}"}})
                else:
                    parts.append({"type": "text", "text": p.get("text", "")})
            out.append({"role": m["role"], "content": parts})
        return out

    def _native_call(self, fn):
        from .gemini_native import GeminiAPIError
        try:
            return fn()
        except GeminiAPIError as e:
            log.warning("Gemini %s hata: status=%s kota=%s değer=%s bekle=%s tür=%s mesaj=%s", self.model, e.status,
                        e.quota_id, e.quota_value, e.retry_after, e.kind, e.message[:300])
            raise _from_gemini(e, self.model) from e

    def complete(self, system: str, messages: list[dict], max_tokens: int = 4096,
                 temperature: float = 0.7, json_mode: bool = False) -> str:
        self.last_finish = ""
        if self._native is not None:
            # düşünme token'ları çıktı bütçesinden düşülür: pay bırak
            mt = min(65536, max_tokens + self.THINKING_HEADROOM)
            text = self._native_call(lambda: self._native.complete(system, messages, mt, temperature, json_mode))
            res = self._native.last
            self.last_usage = res.usage
            from . import usage
            usage.record(self.model)
            self.last_finish = "length" if res.finish == "MAX_TOKENS" else res.finish.lower()
            log.info("Gemini %s: %.1f sn, bitiş=%s, kullanım=%s", self.model, res.secs, res.finish, res.usage)
            return text
        try:
            messages = self._conv(messages)
            if self.provider == "anthropic":
                r = self._client.messages.create(
                    model=self.model, system=system, messages=messages,
                    max_tokens=max_tokens, temperature=temperature,
                )
                self.last_finish = "length" if getattr(r, "stop_reason", "") == "max_tokens" else ""
                text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
                if not text.strip():
                    raise LLMError("Claude boş yanıt döndürdü.", True)
                return text
            kw = self._openai_kwargs(max_tokens, temperature)
            kw["messages"] = [{"role": "system", "content": system}, *messages]
            r = self._openai_create(kw)
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _classify(e, self.provider) from e
        choice = r.choices[0] if r.choices else None
        text = (choice.message.content if choice and choice.message else None) or ""
        self.last_finish = (getattr(choice, "finish_reason", "") or "") if choice else ""
        if not text.strip():
            reason = self.last_finish
            if reason in ("content_filter", "safety", "SAFETY", "prohibited_content"):
                raise LLMError(f"{PROVIDERS[self.provider]['short']} yanıtı güvenlik filtresine takıldı.", True)
            if reason == "length":
                raise LLMError(f"{PROVIDERS[self.provider]['short']} yanıtı uzunluk sınırına takıldı.", True)
            raise LLMError(f"{PROVIDERS[self.provider]['short']} boş yanıt döndürdü.", True)
        return text

    def stream(self, system: str, messages: list[dict], max_tokens: int = 2048,
               temperature: float = 0.4) -> Iterator[str]:
        if self._native is not None:
            from .gemini_native import GeminiAPIError
            mt = min(65536, max_tokens + self.THINKING_HEADROOM)
            try:
                yield from self._native.stream(system, messages, mt, temperature)
                from . import usage
                usage.record(self.model)
            except GeminiAPIError as e:
                log.warning("Gemini %s akış hatası: status=%s kota=%s bekle=%s tür=%s mesaj=%s", self.model,
                            e.status, e.quota_id, e.retry_after, e.kind, e.message[:300])
                raise _from_gemini(e, self.model) from e
            return
        messages = self._conv(messages)
        try:
            if self.provider == "anthropic":
                with self._client.messages.stream(
                    model=self.model, system=system, messages=messages,
                    max_tokens=max_tokens, temperature=temperature,
                ) as s:
                    yield from s.text_stream
                return
            kw = self._openai_kwargs(max_tokens, temperature)
            kw.update(stream=True, messages=[{"role": "system", "content": system}, *messages])
            s = self._openai_create(kw)
            for ev in s:
                if ev.choices and ev.choices[0].delta and ev.choices[0].delta.content:
                    yield ev.choices[0].delta.content
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _classify(e, self.provider) from e

    def complete_json(self, system: str, user, max_tokens: int = 6000,
                      temperature: float = 0.7, retries: int = 1) -> dict:
        """JSON döndürmesi beklenen istek; bozuk JSON'da bir kez düzeltme ister."""
        messages = [{"role": "user", "content": user}]
        last_err = ""
        for _ in range(retries + 1):
            text = self.complete(system, messages, max_tokens=max_tokens, temperature=temperature, json_mode=True)
            try:
                return parse_json(text)
            except ValueError as e:
                last_err = str(e)
                log.warning("%s geçersiz JSON döndürdü (%s, bitiş=%s): %s", self.label, e, self.last_finish,
                            text[:1500])
                if self.last_finish in ("length", "max_tokens"):
                    max_tokens = min(32000, int(max_tokens * 1.8))   # yanıt kesildi: daha geniş bütçeyle tekrar
                    continue
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": "Yanıtın geçerli JSON değildi. Sadece geçerli JSON nesnesini, "
                                                "başka hiçbir metin olmadan tekrar gönder."},
                ]
        raise LLMError(f"{self.label} geçerli JSON üretemedi ({last_err}).", True)


class RoutedLLM:
    """Birincil modeli kullanır. Geçici hatada (yoğunluk, hız sınırı, zaman aşımı) kısa bekleyip bir kez tekrar
    dener; olmazsa aynı sağlayıcının hafif yedek modeline, o da olmazsa diğer sağlayıcılara geçer.

    `used` son başarılı yanıtı veren modelin etiketini, `fell_back` yedeğe geçilip geçilmediğini tutar.
    """

    RETRY_WAIT = 4.0
    # Sınıra takılan / yanıt vermeyen modeller kısa süre atlanır (tüm istemciler arasında ortak)
    _cooldown_until: dict[tuple[str, str], float] = {}

    @classmethod
    def _cool(cls, p: str, m: str, secs: float) -> None:
        if secs > 0:
            cls._cooldown_until[(p, m)] = time.time() + secs

    def _ordered(self) -> list[tuple[str, str]]:
        """Dinlenmedeki modelleri sona atar (hiçbiri müsait değilse yine de dener)."""
        now = time.time()
        from . import usage
        for c in self.candidates:          # bugün günlük kotası dolan model (uygulama yeniden açılsa da) atlanır
            if c[0] == "gemini" and usage.is_exhausted(c[1]) and self._cooldown_until.get(c, 0) <= now:
                self._cooldown_until[c] = now + 1800
        ready = [c for c in self.candidates if self._cooldown_until.get(c, 0) <= now]
        cooling = [c for c in self.candidates if c not in ready]
        return ready + cooling

    def __init__(self, settings: Settings, client_cls=LLMClient, task: str = "heavy"):
        """task: "heavy" (quiz/vaka üretimi — güçlü model) ya da "light" (sohbet, çeviri, puanlama).
        Gemini'de 'kotayı koru' açıksa hafif işler önce hafif modelle yapılır; güçlü modelin günlük kotası
        asıl önemli işlere kalır."""
        self.settings = settings
        self.client_cls = client_cls
        self.task = task
        providers = settings.configured_providers()
        if not providers:
            raise LLMError("Hiç API anahtarı girilmemiş. Ayarlar sekmesinden Claude, Gemini veya OpenAI anahtarı ekleyin.")
        if not settings.fallback_enabled:
            providers = providers[:1] if settings.provider in providers else []
            if not providers:
                p = PROVIDERS[settings.provider]["short"]
                raise LLMError(f"{p} API anahtarı girilmemiş. Ayarlar'dan ekleyin ya da başka bir yapay zekâ seçin.")
        self.providers = providers
        self.candidates: list[tuple[str, str]] = []
        for p in providers:
            primary = settings.model_for(p)
            backup = PROVIDERS[p].get("backup_model")
            light_first = (task == "light" and p == "gemini" and getattr(settings, "gemini_save_quota", True)
                           and backup and backup != primary)
            if light_first:
                self.candidates.append((p, backup))
                if settings.fallback_enabled:
                    self.candidates.append((p, primary))
                continue
            self.candidates.append((p, primary))
            if settings.fallback_enabled and backup and backup != primary:
                self.candidates.append((p, backup))
        self.used: str = ""
        self.fell_back = False
        self.notes: list[str] = []

    @property
    def label(self) -> str:
        if self.used:
            return self.used
        p, m = self.candidates[0]
        return f"{PROVIDERS[p]['short']} · {m}"

    def _make(self, p: str, m: str):
        import inspect
        try:
            n = len(inspect.signature(self.client_cls).parameters)
        except (TypeError, ValueError):
            n = 3
        # eski imzalı (settings, provider) test istemcileri de desteklenir; gerçek TypeError'lar gizlenmez
        return self.client_cls(self.settings, p, m) if n >= 3 else self.client_cls(self.settings, p)

    # Bekleme politikası: sağlayıcı "N sn sonra dene" derse (ör. Gemini dakikalık sınır) ve N makulse beklenir,
    # birincil model hemen yedeğe bırakılmaz. Toplam bekleme bir istek için WAIT_BUDGET'ı aşmaz.
    MAX_WAIT_PER_TRY = 40.0
    WAIT_BUDGET = 75.0
    status_hook = None          # arayüzün durum çubuğu için: callable(str)

    def _status(self, msg: str) -> None:
        log.info(msg)
        hook = type(self).status_hook
        if hook:
            try:
                hook(msg)
            except Exception:  # noqa: BLE001
                pass

    def _attempts(self, p: str, m: str) -> int:
        return 3 if (p, m) == self.candidates[0] else 2

    def _handle(self, e: LLMError, p: str, m: str, attempt: int, waited: list[float]) -> bool:
        """Hata sonrası: True → aynı modeli tekrar dene (gerekirse bekledikten sonra), False → sıradakine geç."""
        self._cool(p, m, getattr(e, "cooldown", 0))
        if not e.transient or not getattr(e, "retry_same", True) or attempt >= self._attempts(p, m):
            return False
        wait = getattr(e, "retry_after", 0) or self.RETRY_WAIT
        if wait > self.MAX_WAIT_PER_TRY or waited[0] + wait > self.WAIT_BUDGET:
            return False
        short = f"{PROVIDERS[p]['short']} · {m}"
        self._status(f"{short} şu an meşgul — {int(round(wait))} sn sonra tekrar deneniyor ({attempt + 1}. deneme)…")
        time.sleep(wait)
        waited[0] += wait
        self._cooldown_until.pop((p, m), None)
        return True

    def _run(self, fn):
        errors: list[str] = []
        dead: set[str] = set()
        order = self._ordered()
        waited = [0.0]
        for i, (p, m) in enumerate(order):
            if p in dead:
                continue
            attempt = 0
            while True:
                attempt += 1
                try:
                    client = self._make(p, m)
                    res = fn(client)
                    self.used = client.label
                    self.fell_back = (p, m) != self.candidates[0]
                    self._cooldown_until.pop((p, m), None)
                    return res
                except LLMError as e:
                    log.warning("%s/%s başarısız (deneme %d): %s", p, m, attempt, e)
                    if self._handle(e, p, m, attempt, waited):
                        continue
                    errors.append(f"{PROVIDERS[p]['short']} · {m}: {e}")
                    if not e.fallback_ok:
                        raise LLMError(str(e)) from e
                    if getattr(e, "provider_wide", False):
                        dead.add(p)
                    break
            nxt = next(((np_, nm) for np_, nm in order[i + 1:] if np_ not in dead), None)
            if nxt:
                self.notes.append(f"{p}/{m} yanıt vermedi → {PROVIDERS[nxt[0]]['short']} · {nxt[1]} deneniyor.")
                self._status(f"{PROVIDERS[p]['short']} · {m} yanıt veremedi ({errors[-1].split(': ', 1)[-1][:80]}) "
                             f"→ {PROVIDERS[nxt[0]]['short']} · {nxt[1]} ile devam ediliyor.")
        if len(errors) > 1:
            raise LLMError("Hiçbir yapay zekâ yanıt veremedi:\n• " + "\n• ".join(errors))
        raise LLMError(errors[0] if errors else "Bilinmeyen hata")

    def complete(self, system: str, messages: list[dict], max_tokens: int = 4096, temperature: float = 0.7) -> str:
        return self._run(lambda c: c.complete(system, messages, max_tokens=max_tokens, temperature=temperature))

    def complete_json(self, system: str, user, max_tokens: int = 6000, temperature: float = 0.7,
                      retries: int = 1) -> dict:
        return self._run(lambda c: c.complete_json(system, user, max_tokens=max_tokens,
                                                   temperature=temperature, retries=retries))

    def stream(self, system: str, messages: list[dict], max_tokens: int = 2048,
               temperature: float = 0.4) -> Iterator[str]:
        """İlk parça gelmeden hata olursa tekrar dener / sıradaki modele geçer; yayın başladıktan sonra geçmez."""
        errors: list[str] = []
        dead: set[str] = set()
        waited = [0.0]
        order = self._ordered()
        for i, (p, m) in enumerate(order):
            if p in dead:
                continue
            attempt = 0
            while True:
                attempt += 1
                started = False
                try:
                    client = self._make(p, m)
                    self.used = client.label
                    self.fell_back = (p, m) != self.candidates[0]
                    for piece in client.stream(system, messages, max_tokens=max_tokens, temperature=temperature):
                        started = True
                        yield piece
                    self._cooldown_until.pop((p, m), None)
                    return
                except LLMError as e:
                    log.warning("%s/%s (akış) başarısız (deneme %d): %s", p, m, attempt, e)
                    if started:
                        raise
                    if self._handle(e, p, m, attempt, waited):
                        continue
                    errors.append(f"{PROVIDERS[p]['short']} · {m}: {e}")
                    if not e.fallback_ok:
                        raise LLMError(str(e)) from e
                    if getattr(e, "provider_wide", False):
                        dead.add(p)
                    break
            nxt = next(((np_, nm) for np_, nm in order[i + 1:] if np_ not in dead), None)
            if nxt:
                self._status(f"{PROVIDERS[p]['short']} · {m} yanıt veremedi → "
                             f"{PROVIDERS[nxt[0]]['short']} · {nxt[1]} ile devam ediliyor.")
        raise LLMError(errors[0] if len(errors) == 1 else "Hiçbir yapay zekâ yanıt veremedi:\n• " + "\n• ".join(errors))


def parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("JSON nesnesi bulunamadı")
    start = min(starts)
    end = text.rfind("}" if text[start] == "{" else "]")
    if end <= start:
        raise ValueError("JSON yarım kalmış (yanıt kesilmiş olabilir)")
    candidate = text[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError as e:
            raise ValueError(str(e)) from e
        return data if isinstance(data, dict) else {"items": data}
    return data if isinstance(data, dict) else {"items": data}


def test_provider(settings: Settings, provider: str) -> dict:
    """Ayarlar > Test: sağlayıcıyla gerçek bir mini soru üretimi dener.

    Birincil model geçici olarak meşgulse (hız sınırı/yoğunluk) bu, anahtarın GEÇERLİ olduğunu gösterir;
    o durumda hafif yedek modeli de dener ve sonucu açıklayan bir not döndürür.
    """
    t0 = time.time()
    models = [settings.model_for(provider)]
    backup = PROVIDERS[provider].get("backup_model")
    if backup and backup not in models:
        models.append(backup)
    notes: list[str] = []
    last: LLMError | None = None
    for m in models:
        try:
            c = LLMClient(settings, provider, m)
            data = c.complete_json(
                "Sen bir tıp eğitimcisisin. Yalnızca geçerli JSON döndür.",
                'Hipoglisemi hakkında 4 şıklı tek bir kısa soru yaz. Biçim: '
                '{"question": "...", "options": ["...","...","...","..."], "answer_index": 0}',
                max_tokens=600, temperature=0.3)
            q = data.get("question")
            if not q and isinstance(data.get("items"), list) and data["items"]:
                q = data["items"][0].get("question") if isinstance(data["items"][0], dict) else ""
            q = str(q or "").strip()
            if not q:
                raise LLMError(f"Yanıt alındı ama beklenen biçimde değil: {str(data)[:200]}", True)
            return {"label": c.label, "secs": time.time() - t0, "question": q, "notes": notes}
        except LLMError as e:
            last = e
            if getattr(e, "provider_wide", False) or not e.fallback_ok:
                raise
            wait = getattr(e, "retry_after", 0)
            if e.transient and 0 < wait <= 40 and getattr(e, "retry_same", True):
                time.sleep(wait)               # dakikalık sınır: önerilen süre kadar bekleyip bir kez daha dene
                try:
                    c = LLMClient(settings, provider, m)
                    data = c.complete_json(
                        "Sen bir tıp eğitimcisisin. Yalnızca geçerli JSON döndür.",
                        'Hipoglisemi hakkında 4 şıklı tek bir kısa soru yaz. Biçim: '
                        '{"question": "...", "options": ["...","...","...","..."], "answer_index": 0}',
                        max_tokens=600, temperature=0.3)
                    q = str(data.get("question") or "").strip()
                    if q:
                        notes.append(f"{m} ilk denemede meşguldü, {int(wait)} sn bekleyince yanıt verdi")
                        return {"label": c.label, "secs": time.time() - t0, "question": q, "notes": notes}
                except LLMError as e2:
                    last = e = e2
            if e.transient:
                notes.append(f"{m} şu an meşgul ({e}); anahtar geçerli")
            else:
                notes.append(f"{m}: {e}")
    raise last or LLMError("Test başarısız")
