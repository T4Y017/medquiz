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
                 cooldown: float = 0.0, retry_same: bool = True):
        super().__init__(msg)
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
        if self.provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic(api_key=key, timeout=self._timeout(), max_retries=1)
        else:
            import openai

            base_url = PROVIDERS[self.provider].get("base_url")
            self._client = openai.OpenAI(api_key=key, base_url=base_url, timeout=self._timeout(), max_retries=1)

    # ------------------------------------------------------------ complete
    @staticmethod
    def _timeout():
        import httpx
        # bağlantı 15 sn; yanıt (veya akışta iki parça arası) en fazla 100 sn — sonra yedek modele geçilir
        return httpx.Timeout(100.0, connect=15.0)

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

    def complete(self, system: str, messages: list[dict], max_tokens: int = 4096,
                 temperature: float = 0.7) -> str:
        self.last_finish = ""
        try:
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

    def complete_json(self, system: str, user: str, max_tokens: int = 6000,
                      temperature: float = 0.7, retries: int = 1) -> dict:
        """JSON döndürmesi beklenen istek; bozuk JSON'da bir kez düzeltme ister."""
        messages = [{"role": "user", "content": user}]
        last_err = ""
        for _ in range(retries + 1):
            text = self.complete(system, messages, max_tokens=max_tokens, temperature=temperature)
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
        ready = [c for c in self.candidates if self._cooldown_until.get(c, 0) <= now]
        cooling = [c for c in self.candidates if c not in ready]
        return ready + cooling

    def __init__(self, settings: Settings, client_cls=LLMClient):
        self.settings = settings
        self.client_cls = client_cls
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
            self.candidates.append((p, primary))
            backup = PROVIDERS[p].get("backup_model")
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
        try:
            return self.client_cls(self.settings, p, m)
        except TypeError:          # eski imzalı test istemcileri
            return self.client_cls(self.settings, p)

    def _run(self, fn):
        errors: list[str] = []
        dead: set[str] = set()
        order = self._ordered()
        n = len(order)
        for i, (p, m) in enumerate(order):
            if p in dead:
                continue
            for attempt in (1, 2):
                try:
                    client = self._make(p, m)
                    res = fn(client)
                    self.used = client.label
                    self.fell_back = (p, m) != self.candidates[0]
                    self._cooldown_until.pop((p, m), None)
                    return res
                except LLMError as e:
                    log.warning("%s/%s başarısız (deneme %d): %s", p, m, attempt, e)
                    self._cool(p, m, getattr(e, "cooldown", 0))
                    if e.transient and attempt == 1 and getattr(e, "retry_same", True):
                        time.sleep(self.RETRY_WAIT)
                        continue
                    errors.append(f"{PROVIDERS[p]['short']} · {m}: {e}")
                    if not e.fallback_ok:
                        raise LLMError(str(e)) from e
                    if getattr(e, "provider_wide", False):
                        dead.add(p)
                    break
            if i < n - 1:
                np_, nm = order[i + 1]
                self.notes.append(f"{p}/{m} yanıt vermedi → {PROVIDERS[np_]['short']} · {nm} deneniyor.")
        if len(errors) > 1:
            raise LLMError("Hiçbir yapay zekâ yanıt veremedi:\n• " + "\n• ".join(errors))
        raise LLMError(errors[0] if errors else "Bilinmeyen hata")

    def complete(self, system: str, messages: list[dict], max_tokens: int = 4096, temperature: float = 0.7) -> str:
        return self._run(lambda c: c.complete(system, messages, max_tokens=max_tokens, temperature=temperature))

    def complete_json(self, system: str, user: str, max_tokens: int = 6000, temperature: float = 0.7,
                      retries: int = 1) -> dict:
        return self._run(lambda c: c.complete_json(system, user, max_tokens=max_tokens,
                                                   temperature=temperature, retries=retries))

    def stream(self, system: str, messages: list[dict], max_tokens: int = 2048,
               temperature: float = 0.4) -> Iterator[str]:
        """İlk parça gelmeden hata olursa tekrar dener / sıradaki modele geçer; yayın başladıktan sonra geçmez."""
        errors: list[str] = []
        n = len(self.candidates)
        dead: set[str] = set()
        for i, (p, m) in enumerate(self._ordered()):
            if p in dead:
                continue
            for attempt in (1, 2):
                started = False
                try:
                    client = self._make(p, m)
                    self.used = client.label
                    self.fell_back = (p, m) != self.candidates[0]
                    for piece in client.stream(system, messages, max_tokens=max_tokens, temperature=temperature):
                        started = True
                        yield piece
                    return
                except LLMError as e:
                    log.warning("%s/%s (akış) başarısız (deneme %d): %s", p, m, attempt, e)
                    self._cool(p, m, getattr(e, "cooldown", 0))
                    if started:
                        raise
                    if e.transient and attempt == 1 and getattr(e, "retry_same", True):
                        time.sleep(self.RETRY_WAIT)
                        continue
                    errors.append(f"{PROVIDERS[p]['short']} · {m}: {e}")
                    if not e.fallback_ok:
                        raise LLMError(str(e)) from e
                    if getattr(e, "provider_wide", False):
                        dead.add(p)
                    break
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
            if e.transient:
                notes.append(f"{m} şu an meşgul ({e}); anahtar geçerli")
            else:
                notes.append(f"{m}: {e}")
    raise last or LLMError("Test başarısız")
