"""Claude / Gemini / OpenAI için ortak istemci arayüzü ve sağlayıcılar arası otomatik yedekleme."""
from __future__ import annotations

import json
import logging
import re
from typing import Iterator

from .config import PROVIDERS, Settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, msg: str, fallback_ok: bool = False):
        super().__init__(msg)
        self.fallback_ok = fallback_ok  # başka bir sağlayıcıyla denemek mantıklı mı?


def _classify(e: Exception, provider: str) -> LLMError:
    msg = str(e)
    low = msg.lower()
    name = PROVIDERS.get(provider, {}).get("short", provider)
    status = getattr(e, "status_code", None)
    if status == 401 or "authentication" in low or "invalid x-api-key" in low or "incorrect api key" in low \
            or "api key not valid" in low or "api_key_invalid" in low:
        return LLMError(f"{name} API anahtarı geçersiz. Ayarlar sekmesinden kontrol edin.", True)
    if "credit" in low or "billing" in low or "quota" in low or "insufficient" in low or "resource_exhausted" in low:
        if provider == "gemini":
            return LLMError("Gemini kullanım sınırına ulaşıldı (ücretsiz katmanda dakikalık ve günlük istek sınırı "
                            "vardır). Bir dakika bekleyip tekrar deneyin; sürekli oluyorsa günlük sınır dolmuştur.", True)
        return LLMError(f"{name} hesabında bakiye/kota yok ya da günlük kota doldu.", True)
    if status == 429 or ("rate" in low and "limit" in low):
        return LLMError(f"{name}: çok sık istek gönderildi (hız sınırı). Biraz bekleyip tekrar deneyin.", True)
    if status == 404 or "not_found" in low or ("model" in low and "not" in low and "found" in low):
        return LLMError(f"{name}: seçilen model bulunamadı. Ayarlar'dan başka bir model seçin.", True)
    if "connection" in low or "timed out" in low or "timeout" in low:
        return LLMError(f"{name} sunucusuna bağlanılamadı. İnternet bağlantınızı kontrol edin.", True)
    if status and status >= 500 or "overloaded" in low or "unavailable" in low:
        return LLMError(f"{name} sunucusu şu an yanıt veremiyor (yoğunluk). Biraz sonra tekrar deneyin.", True)
    if status == 403 or "permission" in low or "location is not supported" in low:
        return LLMError(f"{name}: bu anahtarın erişim izni yok ya da bölge desteklenmiyor.", True)
    return LLMError(f"{name} hatası: {msg[:300]}", False)


class LLMClient:
    """Tek bir sağlayıcıya bağlı istemci."""

    # Sağlayıcının reddettiği isteğe bağlı parametreler (provider, model, param) — bir kez öğrenilir.
    _unsupported: set[tuple[str, str, str]] = set()
    THINKING_HEADROOM = 8192   # Gemini 3 "düşünme" token'ları çıktı bütçesinden yer; JSON yarım kalmasın

    def __init__(self, settings: Settings, provider: str | None = None):
        self.provider = provider or settings.provider
        if self.provider not in PROVIDERS:
            raise LLMError(f"Bilinmeyen sağlayıcı: {self.provider}")
        self.model = settings.model_for(self.provider)
        self.label = settings.label_for(self.provider)
        self.last_finish = ""
        key = settings.key_for(self.provider)
        if not key:
            raise LLMError(f"{PROVIDERS[self.provider]['short']} API anahtarı girilmemiş. Ayarlar sekmesinden ekleyin.",
                           True)
        if self.provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic(api_key=key, timeout=180.0, max_retries=2)
        else:
            import openai

            base_url = PROVIDERS[self.provider].get("base_url")
            self._client = openai.OpenAI(api_key=key, base_url=base_url, timeout=180.0, max_retries=2)

    # ------------------------------------------------------------ complete
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
    """Birincil sağlayıcıyı kullanır; kota/bağlantı/anahtar hatasında sıradaki sağlayıcıya geçer.

    `used` son başarılı yanıtı veren modelin etiketini, `fell_back` yedeğe geçilip geçilmediğini tutar.
    """

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
        self.used: str = ""
        self.fell_back = False
        self.notes: list[str] = []

    @property
    def label(self) -> str:
        return self.used or self.settings.label_for(self.providers[0])

    def _run(self, fn):
        errors: list[str] = []
        for i, p in enumerate(self.providers):
            try:
                client = self.client_cls(self.settings, p)
                res = fn(client)
                self.used = client.label
                self.fell_back = i > 0
                return res
            except LLMError as e:
                errors.append(str(e))
                log.warning("Sağlayıcı %s başarısız: %s", p, e)
                if not e.fallback_ok or i == len(self.providers) - 1:
                    break
                self.notes.append(f"{e} → {PROVIDERS[self.providers[i + 1]]['short']} ile deneniyor.")
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
        """İlk parça gelmeden hata olursa sıradaki sağlayıcıya geçer; yayın başladıktan sonra geçmez."""
        errors: list[str] = []
        for i, p in enumerate(self.providers):
            started = False
            try:
                client = self.client_cls(self.settings, p)
                self.used = client.label
                self.fell_back = i > 0
                for piece in client.stream(system, messages, max_tokens=max_tokens, temperature=temperature):
                    started = True
                    yield piece
                return
            except LLMError as e:
                errors.append(str(e))
                log.warning("Sağlayıcı %s (stream) başarısız: %s", p, e)
                if started or not e.fallback_ok or i == len(self.providers) - 1:
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
