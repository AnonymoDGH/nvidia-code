"""
NVIDIA CODE - AI Tools v3
NIM backend: integrate.api.nvidia.com/v1
"""

import re
import json
import time
import requests
from typing import Dict, List, Optional, Any, Tuple, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from functools import lru_cache

from .base import BaseTool, ToolParameter
from config import API_KEY, API_BASE_URL
from models.registry import AVAILABLE_MODELS, ModelRegistry


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

_SESSION = requests.Session()
_SESSION.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "NVIDIA-CODE/3.0",
})

_THINKING_PATTERNS = re.compile(
    r'<(think|thinking|thought|internal|reflection|scratchpad)>.*?</\1>',
    re.DOTALL | re.IGNORECASE
)

_HTTP_ERRORS = {
    400: "Bad request",
    401: "API key inválida",
    403: "Acceso denegado",
    404: "Modelo no encontrado",
    422: "Payload inválido",
    429: "Rate limit — espera",
    500: "Error interno NIM",
    502: "Bad gateway",
    503: "Servicio caído",
}


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AIResponse:
    success: bool
    model_name: str
    model_key: str
    content: str = ""
    error: str = ""
    response_time: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __str__(self) -> str:
        if not self.success:
            return f"[✗ {self.model_name}] {self.error}"
        meta = f"{self.response_time:.1f}s"
        if self.total_tokens:
            meta += f" | {self.total_tokens} tok"
        if self.finish_reason and self.finish_reason != "stop":
            meta += f" | {self.finish_reason}"
        return f"[✓ {self.model_name} | {meta}]\n\n{self.content}"


@dataclass
class ConversationMessage:
    role: str  # system | user | assistant
    content: str


# ─────────────────────────────────────────────────────────────────────────────
# CORE QUERY
# ─────────────────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    text = _THINKING_PATTERNS.sub('', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _extract(data: dict) -> Tuple[str, int, int, str]:
    """(content, prompt_tokens, completion_tokens, finish_reason)"""
    content = ""
    pt = ct = 0
    finish = ""

    choices = data.get("choices", [])
    if choices:
        choice = choices[0]
        msg = choice.get("message", {})
        content = msg.get("content") or choice.get("text", "")
        finish = choice.get("finish_reason", "")

    usage = data.get("usage", {})
    pt = usage.get("prompt_tokens", 0)
    ct = usage.get("completion_tokens", 0)

    return content, pt, ct, finish


def _nim_request(
    model_id: str,
    messages: List[dict],
    max_tokens: int = 4096,
    temperature: float = 0.6,
    top_p: float = 0.95,
    stream: bool = False,
    thinking: bool = False,
    extra: dict = None,
    timeout: int = 120,
) -> requests.Response:
    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": stream,
    }
    if thinking:
        payload["chat_template_kwargs"] = {"thinking": True}
    if extra:
        payload.update(extra)

    return _SESSION.post(API_BASE_URL, json=payload, timeout=timeout, stream=stream)


def query_model(
    model,
    messages: List[dict],
    max_tokens: int = 4096,
    temperature: float = 0.6,
    stream: bool = False,
) -> AIResponse:
    t0 = time.perf_counter()
    timeout = max(60, min(600, max_tokens // 15))
    model_name = getattr(model, 'name', str(model))
    model_key = str(getattr(model, 'key', ''))
    model_id = getattr(model, 'id', str(model))
    thinking = getattr(model, 'thinking', False)

    try:
        resp = _nim_request(
            model_id=model_id,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
            thinking=thinking,
            timeout=timeout,
        )
        resp.raise_for_status()

        if stream:
            content = _collect_stream(resp)
        else:
            data = resp.json()
            content, pt, ct, finish = _extract(data)

        content = _clean(content)
        if not content:
            return AIResponse(False, model_name, model_key, error="Respuesta vacía")

        usage = data.get("usage", {}) if not stream else {}
        return AIResponse(
            success=True,
            model_name=model_name,
            model_key=model_key,
            content=content,
            response_time=time.perf_counter() - t0,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=data.get("choices", [{}])[0].get("finish_reason", "") if not stream else "",
        )

    except requests.exceptions.Timeout:
        return AIResponse(False, model_name, model_key, error=f"Timeout ({timeout}s)")
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        detail = _HTTP_ERRORS.get(code, f"HTTP {code}")
        try:
            body = e.response.json()
            if "error" in body:
                err = body["error"]
                detail += f": {err.get('message', err)[:120] if isinstance(err, dict) else str(err)[:120]}"
        except Exception:
            pass
        return AIResponse(False, model_name, model_key, error=detail)
    except requests.exceptions.ConnectionError:
        return AIResponse(False, model_name, model_key, error="Connection error")
    except Exception as e:
        return AIResponse(False, model_name, model_key, error=f"{type(e).__name__}: {str(e)[:200]}")


def _collect_stream(resp: requests.Response) -> str:
    parts = []
    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8") if isinstance(line, bytes) else line
        if line.startswith("data: "):
            chunk = line[6:]
            if chunk.strip() == "[DONE]":
                break
            try:
                delta = json.loads(chunk)["choices"][0]["delta"].get("content", "")
                if delta:
                    parts.append(delta)
            except Exception:
                pass
    return "".join(parts)


def stream_model(model, messages: List[dict], max_tokens: int = 4096, temperature: float = 0.6) -> Iterator[str]:
    """Yields content chunks for live streaming."""
    timeout = max(60, min(600, max_tokens // 15))
    try:
        resp = _nim_request(
            model_id=model.id,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
            thinking=getattr(model, 'thinking', False),
            timeout=timeout,
        )
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8") if isinstance(line, bytes) else line
            if line.startswith("data: "):
                chunk = line[6:]
                if chunk.strip() == "[DONE]":
                    return
                try:
                    delta = json.loads(chunk)["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except Exception:
                    pass
    except Exception as e:
        yield f"\n[stream error: {e}]"


# ─────────────────────────────────────────────────────────────────────────────
# TOOL: ConsultAITool
# ─────────────────────────────────────────────────────────────────────────────

class ConsultAITool(BaseTool):
    name = "consult_ai"
    description = "Consulta un modelo NIM con historial de conversación, streaming y control total de parámetros."
    category = "ai"

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "model_key":    ToolParameter("model_key",    "string",  "Clave o ID del modelo",                      required=True),
            "prompt":       ToolParameter("prompt",       "string",  "Prompt / mensaje del usuario",               required=True),
            "system":       ToolParameter("system",       "string",  "System prompt (anula el default del modelo)"),
            "history":      ToolParameter("history",      "string",  "Historial JSON: [{role, content}, ...]"),
            "max_tokens":   ToolParameter("max_tokens",   "integer", "Max tokens (default 4096)"),
            "temperature":  ToolParameter("temperature",  "number",  "Temperatura 0.0-2.0 (default 0.6)"),
            "top_p":        ToolParameter("top_p",        "number",  "Top-p 0.0-1.0 (default 0.95)"),
            "stream":       ToolParameter("stream",       "boolean", "Stream output (default False)"),
            "retries":      ToolParameter("retries",      "integer", "Reintentos en fallo (default 1)"),
        }

    def execute(self, **kwargs) -> str:
        model_key   = kwargs.get("model_key", "")
        prompt      = kwargs.get("prompt", "").strip()
        system      = kwargs.get("system", "")
        history_raw = kwargs.get("history", "")
        max_tokens  = int(kwargs.get("max_tokens") or 4096)
        temperature = float(kwargs.get("temperature") or 0.6)
        top_p       = float(kwargs.get("top_p") or 0.95)
        do_stream   = bool(kwargs.get("stream", False))
        retries     = int(kwargs.get("retries") or 1)

        if not prompt:
            return "[x] prompt requerido"

        registry = ModelRegistry()
        model = registry.get(model_key)
        if not model:
            available = "\n".join(f"  {k}: {v.name}" for k, v in AVAILABLE_MODELS.items())
            return f"[x] Modelo '{model_key}' no existe.\nDisponibles:\n{available}"

        messages = []

        sys_prompt = system or getattr(model, 'system_prompt', '') or ""
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})

        if history_raw:
            try:
                history = json.loads(history_raw) if isinstance(history_raw, str) else history_raw
                messages.extend(history)
            except json.JSONDecodeError:
                pass

        messages.append({"role": "user", "content": prompt})

        for attempt in range(retries + 1):
            if do_stream:
                chunks = list(stream_model(model, messages, max_tokens, temperature))
                content = _clean("".join(chunks))
                return content if content else "[x] Stream vacío"
            
            result = query_model(model, messages, max_tokens, temperature)
            if result.success:
                return str(result)
            if attempt < retries:
                time.sleep(1.5 ** attempt)

        return str(result)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL: MultiAIConsultTool
# ─────────────────────────────────────────────────────────────────────────────

class MultiAIConsultTool(BaseTool):
    name = "consult_multiple_ai"
    description = "Consulta N modelos NIM en paralelo. Modos: full | diff | vote"
    category = "ai"

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "prompt":      ToolParameter("prompt",      "string",  "Prompt para todos los modelos",          required=True),
            "models":      ToolParameter("models",      "array",   "Lista de model_keys (default: 1,2,4)"),
            "system":      ToolParameter("system",      "string",  "System prompt compartido"),
            "max_tokens":  ToolParameter("max_tokens",  "integer", "Max tokens por modelo (default 2048)"),
            "temperature": ToolParameter("temperature", "number",  "Temperatura (default 0.6)"),
            "mode":        ToolParameter("mode",        "string",  "full | diff | vote (default full)",
                                         enum=["full", "diff", "vote"]),
            "timeout":     ToolParameter("timeout",     "integer", "Timeout global segundos (default 90)"),
        }

    def execute(self, **kwargs) -> str:
        prompt      = kwargs.get("prompt", "").strip()
        models_keys = kwargs.get("models") or ["1", "2", "4"]
        system      = kwargs.get("system", "")
        max_tokens  = int(kwargs.get("max_tokens") or 2048)
        temperature = float(kwargs.get("temperature") or 0.6)
        mode        = kwargs.get("mode", "full")
        timeout     = int(kwargs.get("timeout") or 90)

        if not prompt:
            return "[x] prompt requerido"
        if len(models_keys) > 6:
            models_keys = models_keys[:6]

        results = self._run_parallel(prompt, models_keys, system, max_tokens, temperature, timeout)

        dispatch = {"full": self._fmt_full, "diff": self._fmt_diff, "vote": self._fmt_vote}
        return dispatch.get(mode, self._fmt_full)(prompt, results)

    def _run_parallel(self, prompt, keys, system, max_tokens, temperature, timeout) -> List[AIResponse]:
        registry = ModelRegistry()
        results = []

        def _q(key):
            model = registry.get(key)
            if not model:
                return AIResponse(False, f"Model {key}", key, error="No encontrado")
            msgs = []
            sp = system or getattr(model, 'system_prompt', '') or ""
            if sp:
                msgs.append({"role": "system", "content": sp})
            msgs.append({"role": "user", "content": prompt})
            return query_model(model, msgs, max_tokens, temperature)

        with ThreadPoolExecutor(max_workers=min(4, len(keys))) as ex:
            futures = {ex.submit(_q, k): k for k in keys}
            for future in as_completed(futures, timeout=timeout):
                try:
                    results.append(future.result())
                except FuturesTimeout:
                    results.append(AIResponse(False, f"Model {futures[future]}", futures[future], error="Global timeout"))
                except Exception as e:
                    results.append(AIResponse(False, f"Model {futures[future]}", futures[future], error=str(e)))

        return results

    def _fmt_full(self, prompt: str, results: List[AIResponse]) -> str:
        ok = [r for r in results if r.success]
        fail = [r for r in results if not r.success]
        lines = [
            f"[Multi-AI | {len(ok)}/{len(results)} OK | prompt: {prompt[:80]}{'...' if len(prompt)>80 else ''}]",
            "═" * 60
        ]
        for r in sorted(results, key=lambda x: x.response_time):
            lines.append(str(r))
            lines.append("─" * 60)
        return "\n\n".join(lines)

    def _fmt_diff(self, prompt: str, results: List[AIResponse]) -> str:
        """Muestra divergencias entre respuestas."""
        ok = [r for r in results if r.success and r.content]
        if len(ok) < 2:
            return self._fmt_full(prompt, results)

        lines = [f"[Diff Mode | {len(ok)} respuestas]\n"]
        
        # Longitudes
        lines.append("Longitudes:")
        for r in sorted(ok, key=lambda x: len(x.content), reverse=True):
            lines.append(f"  {r.model_name}: {len(r.content)} chars | {r.total_tokens} tok | {r.response_time:.1f}s")

        # Palabras únicas por modelo
        lines.append("\nTérminos exclusivos por modelo:")
        word_sets = {}
        for r in ok:
            words = set(re.findall(r'\b[a-zA-Z]{5,}\b', r.content.lower()))
            word_sets[r.model_name] = words

        all_words = set.union(*word_sets.values()) if word_sets else set()
        for name, ws in word_sets.items():
            unique = ws - set.union(*(word_sets[n] for n in word_sets if n != name))
            top = sorted(unique)[:8]
            lines.append(f"  {name}: {', '.join(top) if top else '(sin términos únicos)'}")

        lines.append("\nPrimeras 200 chars de cada uno:")
        for r in ok:
            lines.append(f"\n[{r.model_name}]\n{r.content[:200]}...")

        return "\n".join(lines)

    def _fmt_vote(self, prompt: str, results: List[AIResponse]) -> str:
        """Votación por longitud + velocidad como heurística de calidad."""
        ok = [r for r in results if r.success and r.content]
        if not ok:
            return "[x] Sin respuestas exitosas para votar"

        scored = []
        max_len = max(len(r.content) for r in ok) or 1
        max_time = max(r.response_time for r in ok) or 1

        for r in ok:
            score = (len(r.content) / max_len) * 0.6 + (1 - r.response_time / max_time) * 0.4
            scored.append((score, r))

        scored.sort(key=lambda x: -x[0])
        winner_score, winner = scored[0]

        lines = [f"[Vote Mode | prompt: {prompt[:60]}...]\n"]
        for score, r in scored:
            bar = "█" * int(score * 20)
            lines.append(f"  {r.model_name:20} {bar} {score:.2f}")

        lines.append(f"\n🏆 Winner: {winner.model_name}")
        lines.append(f"\n{winner.content[:1500]}{'...' if len(winner.content) > 1500 else ''}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL: CompareModelsPerformanceTool
# ─────────────────────────────────────────────────────────────────────────────

class CompareModelsPerformanceTool(BaseTool):
    name = "compare_models"
    description = "Benchmarkea N modelos con el mismo prompt. Tabla de velocidad, tokens y throughput."
    category = "ai"

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "prompt":      ToolParameter("prompt",  "string", "Prompt de benchmark",       required=True),
            "models":      ToolParameter("models",  "array",  "Model keys (default todos disponibles)"),
            "max_tokens":  ToolParameter("max_tokens", "integer", "Max tokens (default 512)"),
        }

    def execute(self, **kwargs) -> str:
        prompt     = kwargs.get("prompt", "").strip()
        keys       = kwargs.get("models") or list(AVAILABLE_MODELS.keys())[:8]
        max_tokens = int(kwargs.get("max_tokens") or 512)

        if not prompt:
            return "[x] prompt requerido"

        multi = MultiAIConsultTool()
        results = multi._run_parallel(prompt, keys, "", max_tokens, 0.6, 120)

        ok = [r for r in results if r.success]
        fail = [r for r in results if not r.success]

        lines = [f"[Benchmark | {len(ok)}/{len(results)} OK | max_tokens={max_tokens}]", ""]
        lines.append(f"{'Modelo':<22} {'Status':^6} {'Time':>6} {'P.tok':>6} {'C.tok':>6} {'tok/s':>7} {'chars':>7}")
        lines.append("─" * 70)

        for r in sorted(results, key=lambda x: x.response_time if x.success else 9999):
            if r.success:
                tps = r.completion_tokens / r.response_time if r.response_time > 0 else 0
                lines.append(
                    f"{r.model_name:<22} {'✓':^6} {r.response_time:>5.1f}s "
                    f"{r.prompt_tokens:>6} {r.completion_tokens:>6} {tps:>6.1f} {len(r.content):>7}"
                )
            else:
                lines.append(f"{r.model_name:<22} {'✗':^6}  {'—':>6} {'—':>6} {'—':>6} {'—':>6} {'—':>7}  {r.error[:30]}")

        if ok:
            fastest = min(ok, key=lambda x: x.response_time)
            best_tps = max(ok, key=lambda x: x.completion_tokens / x.response_time if x.response_time > 0 else 0)
            most_verbose = max(ok, key=lambda x: len(x.content))
            lines += [
                "",
                f"⚡ Más rápido    : {fastest.model_name} ({fastest.response_time:.1f}s)",
                f"🚀 Mayor tok/s   : {best_tps.model_name}",
                f"📄 Más verboso   : {most_verbose.model_name} ({len(most_verbose.content)} chars)",
            ]

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL: ChatSessionTool
# ─────────────────────────────────────────────────────────────────────────────

class ChatSessionTool(BaseTool):
    """Mantiene historial de conversación en memoria por session_id."""
    name = "chat_session"
    description = "Chat multi-turn con historial persistente en memoria. Soporta reset y export."
    category = "ai"

    _sessions: Dict[str, List[dict]] = {}

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "session_id": ToolParameter("session_id", "string", "ID de la sesión",          required=True),
            "model_key":  ToolParameter("model_key",  "string", "Clave del modelo",          required=True),
            "message":    ToolParameter("message",    "string", "Mensaje del usuario"),
            "system":     ToolParameter("system",     "string", "System prompt (solo primera vez)"),
            "max_tokens": ToolParameter("max_tokens", "integer","Max tokens (default 4096)"),
            "action":     ToolParameter("action",     "string", "chat | reset | export | history",
                                        enum=["chat", "reset", "export", "history"]),
        }

    def execute(self, **kwargs) -> str:
        sid        = kwargs.get("session_id", "default")
        model_key  = kwargs.get("model_key", "")
        message    = kwargs.get("message", "").strip()
        system     = kwargs.get("system", "")
        max_tokens = int(kwargs.get("max_tokens") or 4096)
        action     = kwargs.get("action", "chat")

        if action == "reset":
            self._sessions.pop(sid, None)
            return f"[+] Sesión '{sid}' reseteada"

        if action == "history":
            hist = self._sessions.get(sid, [])
            if not hist:
                return f"[i] Sesión '{sid}' vacía"
            return json.dumps(hist, ensure_ascii=False, indent=2)

        if action == "export":
            hist = self._sessions.get(sid, [])
            path = f"session_{sid}_{int(time.time())}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False, indent=2)
            return f"[+] Exportado → {path}"

        # action == "chat"
        if not message:
            return "[x] message requerido para chat"

        registry = ModelRegistry()
        model = registry.get(model_key)
        if not model:
            return f"[x] Modelo '{model_key}' no encontrado"

        if sid not in self._sessions:
            self._sessions[sid] = []
            sp = system or getattr(model, 'system_prompt', '') or ""
            if sp:
                self._sessions[sid].append({"role": "system", "content": sp})

        self._sessions[sid].append({"role": "user", "content": message})

        result = query_model(model, self._sessions[sid], max_tokens)

        if result.success:
            self._sessions[sid].append({"role": "assistant", "content": result.content})
            turns = sum(1 for m in self._sessions[sid] if m["role"] == "user")
            return f"[{model.name} | turn {turns} | {result.response_time:.1f}s]\n\n{result.content}"
        else:
            self._sessions[sid].pop()
            return f"[x] {result.error}"


# ─────────────────────────────────────────────────────────────────────────────
# REGISTER
# ─────────────────────────────────────────────────────────────────────────────

def register_ai_tools(registry):
    for tool in [
        ConsultAITool(),
        MultiAIConsultTool(),
        CompareModelsPerformanceTool(),
        ChatSessionTool(),
    ]:
        registry.register(tool)