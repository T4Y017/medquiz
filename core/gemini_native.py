"""Gemini'nin kendi REST API'si (generateContent / streamGenerateContent).

OpenAI uyumluluk katmanına göre avantajları:
- düşünme düzeyi (thinkingLevel) ve JSON modu (responseMimeType) doğrudan ayarlanır,
- yanıt akışla (SSE) alınır: uzun yanıtlarda bağlantı zaman aşımına düşmez,
- hata gövdesindeki kota türü (dakikalık / günlük) ve önerilen bekleme süresi (retryDelay) okunur.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterator

log = logging.getLogger(__name__)
BASE = "https://generativelanguage.googleapis.com/v1beta"

SAFETY = [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in (
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT")]


class GeminiAPIError(Exception):
    def __init__(self, status: int, message: str, quota_id: str = "", quota_value: str = "",
                 retry_after: float = 0.0, kind: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.quota_id = quota_id
        self.quota_value = quota_value
        self.retry_after = retry_after
        self.kind = kind            # "timeout", "network", "blocked", "length", "empty", ""

    @property
    def daily_quota(self) -> bool:
        return self.status == 429 and "perday" in self.quota_id.lower()


@dataclass
class Result:
    text: str = ""
    finish: str = ""
    usage: dict = field(default_factory=dict)
    secs: float = 0.0


def _parse_error(status: int, body: str) -> GeminiAPIError:
    msg, quota_id, quota_value, retry = body[:300], "", "", 0.0
    try:
        err = json.loads(body).get("error", {})
        msg = err.get("message", msg)
        for d in err.get("details", []) or []:
            t = d.get("@type", "")
            if t.endswith("QuotaFailure"):
                v = (d.get("violations") or [{}])[0]
                quota_id = v.get("quotaId", "") or v.get("quotaMetric", "")
                quota_value = str(v.get("quotaValue", ""))
            elif t.endswith("RetryInfo"):
                m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
                if m:
                    retry = float(m.group(1))
    except (ValueError, AttributeError):
        pass
    if not retry:
        m = re.search(r"retry in ([\d.]+)\s*s", msg, re.I)
        if m:
            retry = float(m.group(1))
    return GeminiAPIError(status, msg, quota_id, quota_value, retry)


class GeminiNative:
    # Modelin reddettiği düşünme ayarları bir kez öğrenilir
    _no_thinking_cfg: set[str] = set()

    def __init__(self, api_key: str, model: str, connect_timeout: float = 15.0, idle_timeout: float = 120.0,
                 total_timeout: float = 300.0, session=None):
        import requests

        self.key = api_key
        self.model = model
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout        # akışta iki parça arası en fazla bekleme
        self.total_timeout = total_timeout
        self.http = session or requests.Session()
        self.last = Result()

    # ------------------------------------------------------------------ istek gövdesi
    def _body(self, system: str, messages: list[dict], max_tokens: int, temperature: float,
              json_mode: bool, thinking: str | None) -> dict:
        import base64

        contents = []
        for m in messages:
            role = "model" if m["role"] == "assistant" else "user"
            c = m["content"]
            if isinstance(c, list):          # çok biçimli: [{"type":"text"|"pdf", ...}]
                parts = []
                for p in c:
                    if p.get("type") == "pdf":
                        parts.append({"inline_data": {"mime_type": "application/pdf",
                                                      "data": base64.b64encode(p["data"]).decode()}})
                    else:
                        parts.append({"text": p.get("text", "")})
            else:
                parts = [{"text": c}]
            if contents and contents[-1]["role"] == role:          # ardışık aynı rol → birleştir
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        gen: dict = {"maxOutputTokens": int(max_tokens), "temperature": float(temperature)}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        if thinking and self.model not in self._no_thinking_cfg:
            gen["thinkingConfig"] = {"thinkingLevel": thinking}
        body = {"contents": contents, "generationConfig": gen, "safetySettings": SAFETY}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.key, "Content-Type": "application/json"}

    # ------------------------------------------------------------------ akış
    def stream(self, system: str, messages: list[dict], max_tokens: int = 4096, temperature: float = 0.7,
               json_mode: bool = False, thinking: str | None = "low") -> Iterator[str]:
        import requests

        url = f"{BASE}/models/{self.model}:streamGenerateContent?alt=sse"
        body = self._body(system, messages, max_tokens, temperature, json_mode, thinking)
        t0 = time.time()
        self.last = Result()
        try:
            r = self.http.post(url, headers=self._headers(), json=body, stream=True,
                               timeout=(self.connect_timeout, self.idle_timeout))
        except requests.Timeout as e:
            raise GeminiAPIError(0, "Gemini zamanında yanıt vermedi.", kind="timeout") from e
        except requests.RequestException as e:
            raise GeminiAPIError(0, f"Gemini sunucusuna bağlanılamadı: {e}", kind="network") from e
        if r.status_code != 200:
            text = r.text
            r.close()
            err = _parse_error(r.status_code, text)
            if (r.status_code == 400 and "thinkingConfig" in body["generationConfig"]
                    and re.search(r"think", err.message, re.I)):
                log.info("%s thinkingLevel=%s kabul etmedi; ayarsız tekrar", self.model, thinking)
                self._no_thinking_cfg.add(self.model)
                yield from self.stream(system, messages, max_tokens, temperature, json_mode, None)
                return
            raise err
        parts: list[str] = []
        finish, usage, blocked = "", {}, ""
        # SSE yanıtı charset belirtmez; requests ISO-8859-1 varsayar ve Türkçe karakterler bozulur ("Ã¼")
        r.encoding = "utf-8"
        try:
            for raw in r.iter_lines(decode_unicode=True):
                if time.time() - t0 > self.total_timeout:
                    raise GeminiAPIError(0, "Gemini yanıtı çok uzun sürdü.", kind="timeout")
                if not raw or not raw.startswith("data:"):
                    continue
                try:
                    ev = json.loads(raw[5:].strip())
                except ValueError:
                    continue
                if "error" in ev:
                    raise _parse_error(int(ev["error"].get("code", 500)), json.dumps(ev))
                pf = ev.get("promptFeedback") or {}
                if pf.get("blockReason"):
                    blocked = pf["blockReason"]
                usage = ev.get("usageMetadata") or usage
                for c in ev.get("candidates") or []:
                    finish = c.get("finishReason") or finish
                    for p in (c.get("content") or {}).get("parts") or []:
                        if p.get("thought"):
                            continue
                        t = p.get("text")
                        if t:
                            parts.append(t)
                            yield t
        except requests.Timeout as e:
            raise GeminiAPIError(0, f"Gemini {int(self.idle_timeout)} sn boyunca veri göndermedi.",
                                 kind="timeout") from e
        except requests.RequestException as e:
            raise GeminiAPIError(0, f"Bağlantı koptu: {e}", kind="network") from e
        finally:
            r.close()
        self.last = Result("".join(parts), finish, usage, time.time() - t0)
        if blocked or finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"):
            if not parts:
                raise GeminiAPIError(200, f"Yanıt güvenlik filtresine takıldı ({blocked or finish}).", kind="blocked")
        if not parts:
            if finish == "MAX_TOKENS":
                raise GeminiAPIError(200, "Yanıt, düşünme aşamasında uzunluk sınırına takıldı.", kind="length")
            raise GeminiAPIError(200, f"Gemini boş yanıt döndürdü (bitiş: {finish or '?'}).", kind="empty")

    def complete(self, system: str, messages: list[dict], max_tokens: int = 4096, temperature: float = 0.7,
                 json_mode: bool = False, thinking: str | None = "low") -> str:
        """Akışla alır, birleştirip döndürür (uzun yanıtlarda zaman aşımı riskini azaltır)."""
        return "".join(self.stream(system, messages, max_tokens, temperature, json_mode, thinking))

    def model_info(self) -> dict:
        r = self.http.get(f"{BASE}/models/{self.model}", headers=self._headers(),
                          timeout=(self.connect_timeout, 30))
        if r.status_code != 200:
            raise _parse_error(r.status_code, r.text)
        d = r.json()
        return {k: d.get(k) for k in ("name", "displayName", "inputTokenLimit", "outputTokenLimit", "thinking",
                                       "supportedGenerationMethods")}
