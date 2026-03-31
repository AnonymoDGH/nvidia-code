"""
NVIDIA CODE - Arsenal Tools v2
"""

import os
import re
import json
import asyncio
import requests
from bs4 import BeautifulSoup
from typing import Dict, List, Any, Optional
from urllib.parse import urljoin, urlparse, quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed

from tools.base import BaseTool, ToolParameter
from ui.colors import C

try:
    from playwright.async_api import async_playwright
    HAS_PW = True
except ImportError:
    HAS_PW = False


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


class ScrapingAgresivoTool(BaseTool):
    name = "scraping_agresivo"
    description = "Bypass Cloudflare/bot-detect con Playwright stealth. Retorna HTML limpio + metadatos."
    category = "security"

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "url": ToolParameter("url", "string", "URL objetivo", required=True),
            "wait_selector": ToolParameter("wait_selector", "string", "Selector CSS opcional para esperar"),
            "extract_links": ToolParameter("extract_links", "boolean", "Si extrae todos los hrefs (default False)"),
            "js_eval": ToolParameter("js_eval", "string", "JS a evaluar en la página antes de extraer"),
        }

    def execute(self, **kwargs) -> str:
        if not HAS_PW:
            return "[x] playwright no instalado: pip install playwright && playwright install chromium"
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(self._run(**kwargs))

    async def _run(self, url: str, wait_selector: str = None, extract_links: bool = False, js_eval: str = None) -> str:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-infobars',
                    '--window-size=1920,1080',
                ]
            )
            ctx = await browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
                window.chrome = {runtime: {}};
            """)
            page = await ctx.new_page()

            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=10000)
                except Exception:
                    pass

            if js_eval:
                try:
                    js_result = await page.evaluate(js_eval)
                except Exception as e:
                    js_result = f"JS eval error: {e}"
            else:
                js_result = None

            content = await page.content()
            title = await page.title()
            final_url = page.url
            await browser.close()

        soup = BeautifulSoup(content, 'html.parser')
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = re.sub(r'\n{3,}', '\n\n', soup.get_text(separator='\n')).strip()

        out = [
            f"[+] {final_url}",
            f"[i] Title: {title} | Tamaño: {len(content)/1024:.1f} KB",
            f"\n--- TEXT PREVIEW (2000 chars) ---\n{text[:2000]}",
        ]

        if extract_links:
            links = list({a['href'] for a in soup.find_all('a', href=True)})
            links = [urljoin(url, l) for l in links if not l.startswith('#')]
            out.append(f"\n--- LINKS ({len(links)}) ---\n" + "\n".join(links[:30]))

        if js_result is not None:
            out.append(f"\n--- JS EVAL ---\n{js_result}")

        return "\n".join(out)


class LLMSlaveTool(BaseTool):
    name = "esclavizador_llm"
    description = "Consulta LLMs via API: Ollama local, NVIDIA NIM, o OpenRouter (free tier). Configurable."
    category = "ai"

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "prompt": ToolParameter("prompt", "string", "Prompt para el modelo", required=True),
            "backend": ToolParameter("backend", "string", "ollama | nim | openrouter (default: ollama)"),
            "model": ToolParameter("model", "string", "Nombre del modelo (default según backend)"),
            "system": ToolParameter("system", "string", "System prompt opcional"),
        }

    _BACKENDS = {
        "ollama": {
            "url": "http://localhost:11434/api/chat",
            "default_model": "llama3",
        },
        "nim": {
            "url": "https://integrate.api.nvidia.com/v1/chat/completions",
            "default_model": "meta/llama-3.1-8b-instruct",
        },
        "openrouter": {
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "default_model": "mistralai/mistral-7b-instruct:free",
        },
    }

    def execute(self, **kwargs) -> str:
        prompt = kwargs["prompt"]
        backend = kwargs.get("backend", "ollama").lower()
        system = kwargs.get("system", "You are a helpful assistant.")

        cfg = self._BACKENDS.get(backend)
        if not cfg:
            return f"[x] Backend inválido. Opciones: {list(self._BACKENDS.keys())}"

        model = kwargs.get("model") or cfg["default_model"]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

        try:
            if backend == "ollama":
                r = requests.post(cfg["url"], json={"model": model, "messages": messages, "stream": False}, timeout=60)
                r.raise_for_status()
                return r.json()["message"]["content"]
            else:
                api_key = os.environ.get("NVIDIA_API_KEY" if backend == "nim" else "OPENROUTER_API_KEY", "")
                r = requests.post(
                    cfg["url"],
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "max_tokens": 1024},
                    timeout=60
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except requests.exceptions.ConnectionError:
            return f"[x] {backend} no disponible en {cfg['url']}"
        except Exception as e:
            return f"[x] Error {backend}: {e}"


class OSINTDoxTool(BaseTool):
    name = "osint_auto"
    description = "OSINT pasivo: DuckDuckGo dorks, GitHub, crt.sh (cert transparency), HaveIBeenPwned check, Hunter.io public."
    category = "security"

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "target": ToolParameter("target", "string", "Email, usuario, dominio o IP", required=True),
            "deep": ToolParameter("deep", "boolean", "Activa fuentes extra (más lento, default False)"),
        }

    def execute(self, **kwargs) -> str:
        target = kwargs["target"]
        deep = kwargs.get("deep", False)
        results = [f"[OSINT] Target: {target}\n{'='*40}"]

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {
                ex.submit(self._ddg, target): "DDG",
                ex.submit(self._github, target): "GitHub",
                ex.submit(self._crtsh, target): "crt.sh",
            }
            if deep:
                futures[ex.submit(self._hibp, target)] = "HIBP"

            for future, source in futures.items():
                try:
                    res = future.result(timeout=15)
                    if res:
                        results.append(f"[{source}]\n{res}")
                except Exception as e:
                    results.append(f"[{source}] Error: {e}")

        return "\n\n".join(results)

    def _ddg(self, target: str) -> str:
        dorks = [
            f'"{target}"',
            f'"{target}" site:linkedin.com OR site:twitter.com OR site:github.com',
            f'"{target}" filetype:pdf OR filetype:csv',
        ]
        out = []
        for q in dorks[:2]:
            try:
                r = requests.get(
                    f"https://html.duckduckgo.com/html/?q={quote_plus(q)}",
                    headers=_HEADERS, timeout=10
                )
                soup = BeautifulSoup(r.text, 'html.parser')
                links = [a.get_text(strip=True) for a in soup.select('a.result__url')][:5]
                out.extend(links)
            except Exception:
                pass
        return "\n".join(dict.fromkeys(out)) if out else "Sin resultados"

    def _github(self, target: str) -> str:
        parts = []
        username = target.split('@')[0] if '@' in target else target
        try:
            u = requests.get(f"https://api.github.com/users/{username}", headers=_HEADERS, timeout=8).json()
            if 'login' in u:
                parts.append(f"User: {u['html_url']} | Name: {u.get('name')} | Bio: {u.get('bio')} | Repos: {u.get('public_repos')} | Location: {u.get('location')}")
        except Exception:
            pass
        if '@' in target:
            try:
                s = requests.get(f"https://api.github.com/search/users?q={quote_plus(target)}+in:email", headers=_HEADERS, timeout=8).json()
                for item in s.get('items', [])[:3]:
                    parts.append(f"Email match: {item['html_url']}")
            except Exception:
                pass
        return "\n".join(parts) if parts else "No encontrado"

    def _crtsh(self, target: str) -> str:
        domain = target if '.' in target and '@' not in target else None
        if not domain:
            return "N/A (requiere dominio)"
        try:
            r = requests.get(f"https://crt.sh/?q=%.{domain}&output=json", headers=_HEADERS, timeout=10)
            certs = r.json()
            subdomains = list({c['name_value'].replace('*.', '') for c in certs if 'name_value' in c})[:20]
            return f"{len(subdomains)} subdominios:\n" + "\n".join(sorted(subdomains))
        except Exception as e:
            return f"Error: {e}"

    def _hibp(self, target: str) -> str:
        if '@' not in target:
            return "N/A (requiere email)"
        try:
            r = requests.get(
                f"https://haveibeenpwned.com/api/v3/breachedaccount/{quote_plus(target)}",
                headers={**_HEADERS, "hibp-api-key": os.environ.get("HIBP_API_KEY", "")},
                timeout=10
            )
            if r.status_code == 200:
                breaches = [b['Name'] for b in r.json()]
                return f"⚠️ En {len(breaches)} breaches: {', '.join(breaches)}"
            elif r.status_code == 404:
                return "✅ No encontrado en breaches conocidos"
            return f"Status: {r.status_code}"
        except Exception as e:
            return f"Error: {e}"


class PentestExploitTool(BaseTool):
    name = "pentest_scanner"
    description = "Scanner de vulns web: SQLi, XSS, LFI, open redirect. GET y POST. Solo usar en targets propios/autorizados."
    category = "security"

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "url": ToolParameter("url", "string", "URL con parámetro ej. http://site.com/page?id=", required=True),
            "method": ToolParameter("method", "string", "get | post (default: get)"),
            "param": ToolParameter("param", "string", "Nombre del param para POST ej. 'id'"),
        }

    _PAYLOADS = {
        "sqli": [
            ("'", ["error in your sql syntax", "unclosed quotation", "mysql_fetch", "ORA-", "SQLite"]),
            ("1 OR 1=1--", ["admin", "root", "SELECT"]),
            ("1' AND SLEEP(0)--", []),
        ],
        "xss": [
            ("<script>alert(1)</script>", ["<script>alert(1)</script>"]),
            ('"><img src=x onerror=alert(1)>', ['onerror=alert']),
            ("javascript:alert(1)", ["javascript:alert"]),
        ],
        "lfi": [
            ("../../etc/passwd", ["root:x:", "daemon:"]),
            ("....//....//etc/passwd", ["root:x:"]),
        ],
        "redirect": [
            ("//evil.com", ["evil.com"]),
            ("https://evil.com", ["evil.com"]),
        ],
    }

    def execute(self, **kwargs) -> str:
        url = kwargs["url"]
        method = kwargs.get("method", "get").lower()
        param = kwargs.get("param", "input")
        findings = []

        for vuln_type, payloads in self._PAYLOADS.items():
            for payload, triggers in payloads:
                try:
                    if method == "post":
                        r = requests.post(url, data={param: payload}, headers=_HEADERS, timeout=6, allow_redirects=False)
                    else:
                        r = requests.get(f"{url}{payload}", headers=_HEADERS, timeout=6, allow_redirects=False)

                    body = r.text.lower()
                    hit = any(t.lower() in body for t in triggers) if triggers else False
                    redirect_hit = vuln_type == "redirect" and r.status_code in (301, 302) and any(t in r.headers.get('Location', '') for t in triggers)

                    if hit or redirect_hit:
                        findings.append(f"[!!] {vuln_type.upper()} — payload: {payload!r} → status {r.status_code}")
                    
                except requests.exceptions.Timeout:
                    if vuln_type == "sqli" and "SLEEP" in payload:
                        findings.append(f"[?] Posible blind SQLi time-based (timeout en SLEEP payload)")
                except Exception:
                    pass

        if not findings:
            return f"[-] No se detectaron vulns obvias en {url}"
        return f"[SCAN] {url}\n" + "\n".join(findings)


class CloneFrontendTool(BaseTool):
    name = "clonador_frontend"
    description = "Clona frontend: HTML + CSS inline + imágenes como base64 o URLs absolutas. Output listo para usar."
    category = "web"

    @property
    def parameters(self) -> Dict[str, ToolParameter]:
        return {
            "url": ToolParameter("url", "string", "URL a clonar", required=True),
            "inline_css": ToolParameter("inline_css", "boolean", "Inlinea todo el CSS en <style> (default True)"),
            "output": ToolParameter("output", "string", "Ruta de salida (default ./clonado.html)"),
        }

    def execute(self, **kwargs) -> str:
        url = kwargs["url"]
        inline_css = kwargs.get("inline_css", True)
        output = kwargs.get("output", os.path.join(os.getcwd(), "clonado.html"))

        try:
            r = requests.get(url, headers=_HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')

            for tag in soup(["script", "noscript", "iframe"]):
                tag.decompose()

            if inline_css:
                all_css = []
                for link in soup.find_all('link', rel=lambda x: x and 'stylesheet' in x):
                    href = link.get('href')
                    if href:
                        try:
                            css_url = urljoin(url, href)
                            css_r = requests.get(css_url, headers=_HEADERS, timeout=8)
                            all_css.append(css_r.text)
                            link.decompose()
                        except Exception:
                            link['href'] = urljoin(url, href)
                
                if all_css:
                    style_tag = soup.new_tag('style')
                    style_tag.string = "\n".join(all_css)
                    if soup.head:
                        soup.head.append(style_tag)
            else:
                for link in soup.find_all('link', href=True):
                    link['href'] = urljoin(url, link['href'])

            for img in soup.find_all('img', src=True):
                img['src'] = urljoin(url, img['src'])

            for a in soup.find_all('a', href=True):
                if not a['href'].startswith(('http', '#', 'mailto')):
                    a['href'] = urljoin(url, a['href'])

            html = soup.prettify()
            os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
            with open(output, "w", encoding="utf-8") as f:
                f.write(html)

            return f"[+] Clonado → {output} ({len(html)/1024:.1f} KB) | CSS {'inlineado' if inline_css else 'URL-absoluto'}"
        except Exception as e:
            return f"[x] Error: {e}"


def register_arsenal_tools(registry):
    for tool in [
        ScrapingAgresivoTool(),
        LLMSlaveTool(),
        OSINTDoxTool(),
        PentestExploitTool(),
        CloneFrontendTool(),
    ]:
        registry.register(tool)