"""Speech-to-text for Telegram voice notes, only with a key Relay already has.

OpenAI (OPENAI_API_KEY) uses its transcription endpoint; Gemini (GEMINI_API_KEY or GOOGLE_API_KEY) transcribes through
generateContent. Anthropic's API has no speech-to-text, so an Anthropic key alone means "not supported". Keys come
from Relay's environment or from Settings → Agents → agent environment. Nothing is guessed: without a usable key the
bot says so.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import urllib.request

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _keys(cfg: dict) -> dict:
    found = {}
    envs = [os.environ] + [v for v in ((cfg or {}).get("agent_env") or {}).values() if isinstance(v, dict)]
    for env in envs:
        for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            if env.get(name) and name not in found:
                found[name] = str(env[name])
    return found


def provider(cfg: dict) -> dict | None:
    """{"name": "openai"|"gemini", "key": …} or None."""
    k = _keys(cfg)
    if k.get("OPENAI_API_KEY"):
        return {"name": "openai", "key": k["OPENAI_API_KEY"], "model": (cfg or {}).get("transcription_model_openai") or "gpt-4o-mini-transcribe"}
    if k.get("GEMINI_API_KEY") or k.get("GOOGLE_API_KEY"):
        return {"name": "gemini", "key": k.get("GEMINI_API_KEY") or k["GOOGLE_API_KEY"], "model": (cfg or {}).get("transcription_model_gemini") or "gemini-2.5-flash"}
    return None


def _post(url: str, body: bytes, headers: dict, timeout: float = 90) -> dict:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2 * 1024 * 1024).decode("utf-8", "replace") or "{}")


def transcribe(p: dict, audio: bytes, mime: str = "audio/ogg", post=_post) -> str:
    if p["name"] == "openai":
        boundary = "relay" + secrets.token_hex(8)
        parts = []
        for name, value in (("model", p["model"]), ("response_format", "json")):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.ogg\"\r\nContent-Type: {mime}\r\n\r\n".encode()
                     + audio + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        out = post(OPENAI_URL, body, {"Authorization": f"Bearer {p['key']}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
        return str(out.get("text") or "").strip()
    if p["name"] == "gemini":
        payload = {"contents": [{"parts": [{"text": "Transcribe this voice message verbatim. Reply with the transcript only."},
                                           {"inline_data": {"mime_type": mime.split(";")[0], "data": base64.b64encode(audio).decode()}}]}]}
        out = post(GEMINI_URL.format(model=p["model"]), json.dumps(payload).encode(), {"Content-Type": "application/json", "x-goog-api-key": p["key"]})
        cands = out.get("candidates") or []
        texts = [x.get("text") or "" for x in ((cands[0].get("content") or {}).get("parts") or [])] if cands else []
        return " ".join(t.strip() for t in texts if t.strip())
    raise ValueError("unsupported transcription provider")
