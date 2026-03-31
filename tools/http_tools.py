"""
NVIDIA CODE - Cliente HTTP y API Tester v2
"""

import json
import time
from typing import Dict, Any, Optional
from urllib.parse import urljoin

from .base import BaseTool, ToolParameter

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


def _build_session(retries: int = 0, verify_ssl: bool = True) -> "requests.Session":
    s = requests.Session()
    if retries > 0:
        retry = Retry(total=retries, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
    s.verify = verify_ssl
    return s


class HTTPRequestTool(BaseTool):
    name = "http_request"
    description = "Peticiones HTTP completas: GET/POST/PUT/DELETE/PATCH con headers, body, auth, cookies, retries."
    category = "http"

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "url":          ToolParameter("url",          "string",  "URL del endpoint",                        required=True),
            "method":       ToolParameter("method",       "string",  "GET|POST|PUT|DELETE|PATCH (default GET)",  required=False),
            "headers":      ToolParameter("headers",      "object",  "Headers como JSON",                       required=False),
            "body":         ToolParameter("body",         "string",  "Body (JSON string o raw)",                required=False),
            "params":       ToolParameter("params",       "object",  "Query params como JSON",                  required=False),
            "auth":         ToolParameter("auth",         "string",  "Bearer token o 'user:pass' para Basic",   required=False),
            "cookies":      ToolParameter("cookies",      "object",  "Cookies como JSON",                       required=False),
            "timeout":      ToolParameter("timeout",      "integer", "Timeout en segundos (default 30)",        required=False),
            "retries":      ToolParameter("retries",      "integer", "Reintentos en 5xx (default 0)",           required=False),
            "verify_ssl":   ToolParameter("verify_ssl",   "boolean", "Verificar SSL (default True)",            required=False),
            "follow_redirects": ToolParameter("follow_redirects", "boolean", "Seguir redirects (default True)", required=False),
            "raw_output":   ToolParameter("raw_output",   "boolean", "Devuelve solo body sin formato (default False)", required=False),
        }

    def execute(self, **kwargs) -> str:
        if not HAS_REQUESTS:
            return "[x] pip install requests"

        url             = kwargs.get("url", "")
        method          = (kwargs.get("method", "GET") or "GET").upper()
        headers         = kwargs.get("headers") or {}
        body            = kwargs.get("body")
        params          = kwargs.get("params") or {}
        auth_str        = kwargs.get("auth")
        cookies         = kwargs.get("cookies") or {}
        timeout         = int(kwargs.get("timeout") or 30)
        retries         = int(kwargs.get("retries") or 0)
        verify_ssl      = kwargs.get("verify_ssl", True)
        follow_redir    = kwargs.get("follow_redirects", True)
        raw_output      = kwargs.get("raw_output", False)

        if not url:
            return "[x] url requerida"

        if isinstance(headers, str):
            try: headers = json.loads(headers)
            except: headers = {}

        if isinstance(params, str):
            try: params = json.loads(params)
            except: params = {}

        if isinstance(cookies, str):
            try: cookies = json.loads(cookies)
            except: cookies = {}

        json_body = None
        raw_body  = None
        if body:
            try:
                json_body = json.loads(body)
                if "Content-Type" not in headers:
                    headers["Content-Type"] = "application/json"
            except:
                raw_body = body

        auth = None
        if auth_str:
            if ":" in auth_str and not auth_str.lower().startswith("bearer"):
                u, p = auth_str.split(":", 1)
                auth = (u, p)
            else:
                token = auth_str.replace("Bearer ", "").replace("bearer ", "").strip()
                headers["Authorization"] = f"Bearer {token}"

        session = _build_session(retries=retries, verify_ssl=verify_ssl)
        t0 = time.perf_counter()

        try:
            resp = session.request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
                data=raw_body,
                params=params if params else None,
                auth=auth,
                cookies=cookies,
                timeout=timeout,
                allow_redirects=follow_redir,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if raw_output:
                try:
                    return json.dumps(resp.json(), ensure_ascii=False, indent=2)
                except:
                    return resp.text

            return self._format(resp, elapsed_ms)

        except requests.exceptions.Timeout:
            return f"[x] Timeout ({timeout}s)"
        except requests.exceptions.SSLError as e:
            return f"[x] SSL Error (usa verify_ssl=false si es self-signed): {e}"
        except requests.exceptions.ConnectionError as e:
            return f"[x] Connection error: {e}"
        except Exception as e:
            return f"[x] {type(e).__name__}: {e}"

    def _format(self, resp, elapsed_ms: float) -> str:
        from ui.colors import Colors
        C = Colors()

        status_color = (
            C.BRIGHT_GREEN  if resp.status_code < 300 else
            C.BRIGHT_YELLOW if resp.status_code < 400 else
            C.BRIGHT_RED    if resp.status_code < 500 else
            C.RED
        )

        lines = [
            f"{C.NVIDIA_GREEN}┌─ {resp.request.method} {resp.url}{C.RESET}",
            f"{C.NVIDIA_GREEN}│{C.RESET}  Status  : {status_color}{resp.status_code} {resp.reason}{C.RESET}",
            f"{C.NVIDIA_GREEN}│{C.RESET}  Time    : {elapsed_ms:.0f}ms",
            f"{C.NVIDIA_GREEN}│{C.RESET}  Size    : {len(resp.content):,} bytes",
            f"{C.NVIDIA_GREEN}│{C.RESET}  Encoding: {resp.encoding or 'N/A'}",
        ]

        if resp.history:
            lines.append(f"{C.NVIDIA_GREEN}│{C.RESET}  Redirects: {len(resp.history)} → {resp.url}")

        lines.append(f"{C.NVIDIA_GREEN}├─ Headers{C.RESET}")
        interesting = {"content-type","content-length","server","x-ratelimit-remaining","x-powered-by","set-cookie","location","www-authenticate"}
        for k, v in resp.headers.items():
            if k.lower() in interesting:
                lines.append(f"{C.NVIDIA_GREEN}│{C.RESET}  {C.DIM}{k}: {v[:80]}{C.RESET}")

        lines.append(f"{C.NVIDIA_GREEN}├─ Body{C.RESET}")
        try:
            body_json = resp.json()
            formatted = json.dumps(body_json, indent=2, ensure_ascii=False).split('\n')
            cap = 50
            for line in formatted[:cap]:
                lines.append(f"{C.NVIDIA_GREEN}│{C.RESET}  {line}")
            if len(formatted) > cap:
                lines.append(f"{C.NVIDIA_GREEN}│{C.RESET}  {C.DIM}... +{len(formatted)-cap} líneas{C.RESET}")
        except Exception:
            for line in resp.text[:2000].split('\n')[:30]:
                lines.append(f"{C.NVIDIA_GREEN}│{C.RESET}  {line[:100]}")

        lines.append(f"{C.NVIDIA_GREEN}└{'─'*50}{C.RESET}")
        return "\n".join(lines)