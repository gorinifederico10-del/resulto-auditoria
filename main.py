import os
import io
import re
import csv
import json
import time
import string
import secrets
import sqlite3
import asyncio
import threading
import requests
from PIL import Image
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import StreamingResponse, Response, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright
import google.generativeai as genai

# ─────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Falta GEMINI_API_KEY en el archivo .env")

genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel(
    "gemini-2.5-flash",
    generation_config={
        "response_mime_type": "application/json",
        # Determinismo: temperatura 0 + topP/topK acotados → mismo input ≈ mismo output.
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 1,
    },
)

app = FastAPI(title="RESULTO Auditoría")

# CORS: la web de Next.js (en :3000 local, o resulto.com.ar en prod)
# le va a pegar a este backend desde otro origen.
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,https://resulto.com.ar,https://www.resulto.com.ar",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def root():
    """Sirve el index.html (modo in-house, antes de integrar con Next.js)."""
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "resulto-auditoria"}

# ─────────────────────────────────────────────────────────────
# Base de datos SQLite (leads + auditorías guardadas)
# ─────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "auditorias.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "fede-cambia-esto-cuando-deploys")
db_lock = threading.Lock()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auditorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                share_id TEXT UNIQUE NOT NULL,
                email TEXT NOT NULL,
                url TEXT NOT NULL,
                rubro TEXT,
                tiempo TEXT,
                publicidad TEXT,
                objetivo TEXT,
                score INTEGER,
                resultado_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_share_id ON auditorias(share_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email ON auditorias(email)")
        conn.commit()


init_db()

# Cuánto tiempo cacheamos una auditoría por URL (en horas).
# Si el mismo dominio se audita varias veces en este lapso, devolvemos el mismo resultado.
CACHE_HOURS = int(os.getenv("CACHE_HOURS", "24"))


def normalizar_url(url: str) -> str:
    """Quita https/http, www y trailing slashes para comparar URLs como iguales."""
    u = (url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def buscar_auditoria_reciente(url: str) -> dict | None:
    """Si hay una auditoría guardada para esta URL en las últimas CACHE_HOURS horas, la devuelve."""
    norm = normalizar_url(url)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            SELECT resultado_json FROM auditorias
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            """,
            (f"-{CACHE_HOURS} hours",),
        )
        for (payload,) in cursor:
            try:
                data = json.loads(payload)
                # Saltamos rows que vinieron con análisis de competencia adjunto:
                # no se cachean para no contaminar futuras corridas individuales.
                if data.get("competidor"):
                    continue
                # Comparamos por URL normalizada — la guardamos en el JSON al cachear.
                cached_url = data.get("__cache_url_norm") or normalizar_url(
                    data.get("url_analizada", "")
                )
                if cached_url == norm:
                    # Limpiamos el campo interno antes de devolver
                    data.pop("__cache_url_norm", None)
                    # El share_id viejo se va — el caller asigna uno nuevo al volver a guardar.
                    data.pop("share_id", None)
                    return data
            except Exception:
                continue
    return None


def gen_share_id() -> str:
    """Genera un ID corto tipo 'x7K2mP' (6 caracteres alfanuméricos)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


def guardar_auditoria(
    email: str, url: str, rubro: str, tiempo: str,
    publicidad: str, objetivo: str, resultado: dict
) -> str:
    """Inserta el lead + resultado en la DB. Devuelve el share_id."""
    score = int(resultado.get("score", 0) or 0)
    payload = json.dumps(resultado, ensure_ascii=False)
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            for _ in range(5):
                share_id = gen_share_id()
                try:
                    conn.execute(
                        """
                        INSERT INTO auditorias
                          (share_id, email, url, rubro, tiempo, publicidad, objetivo, score, resultado_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (share_id, email, url, rubro, tiempo, publicidad, objetivo, score, payload),
                    )
                    conn.commit()
                    return share_id
                except sqlite3.IntegrityError:
                    continue
    raise RuntimeError("No pude generar un share_id único")

# ─────────────────────────────────────────────────────────────
# Prompt maestro
# ─────────────────────────────────────────────────────────────
PROMPT_MAESTRO = """
Sos un consultor SENIOR de RESULTO, agencia de marketing digital argentina premium. Estás auditando la web de un negocio con criterio DURO. Has visto miles de sitios. Sos HONESTO, no simpático.

🚨 REGLA DE ORO DEL SCORING:
- La web PROMEDIO de una pyme saca 3-4 sobre 10.
- Solo el TOP 20% de las webs merecen 6 o más.
- Solo el TOP 5% merece 8 o más.
- 9-10 está RESERVADO para webs de nivel internacional premium (Apple, Stripe, Notion).
- Si una web te parece "mediocre", "básica", "vieja" o "genérica" → eso es 2-3, NO 5 ni 6.
- NO te quedes en el medio. Usá todo el rango.

TU ROL:
- Hablás como un consultor profesional con un dueño de negocio. Tono claro, directo y respetuoso.
- Nunca usás jerga técnica innecesaria (nada de "meta pixel", "CRO", "GA4", "core web vitals", "bounce rate"). Si tenés que mencionar un concepto, explicalo en una línea.
- Traducís todo a impacto en facturación, en clientes o en tiempo.
- Sos directo y honesto. No exagerás ni edulcorás.
- Registro: rioplatense ("vos") pero profesional. Evitá expresiones callejeras o demasiado coloquiales como "tirar guita", "una papa", "estás al horno", "volás a ciegas".

DATOS DEL NEGOCIO:
- URL: {url}
- Rubro: {rubro}
- Tiempo en el mercado: {tiempo}
- Hace publicidad: {publicidad}
- Objetivo principal: {objetivo}

CONTENIDO SCRAPEADO:
{contenido}

{extras}

⚠️ TENÉS CAPTURAS DE LA WEB (desktop top, medio, footer, y mobile). MIRALAS. Evaluá diseño, tipografía, modernidad, jerarquía, color, espaciado, sensación premium o amateur, si es responsive en celular.

═══════════════════════════════════════════════════════════
DIMENSIONES (0-10 cada una, honest y duro)
═══════════════════════════════════════════════════════════

1. **diseno_visual** (0-10): Estética, tipografía, color, calidad visual. 9-10 nivel Apple/Stripe. 6-8 moderno cuidado. 3-5 plantilla genérica. 1-2 feo, desordenado, tipografías mezcladas.

2. **experiencia_moderna** (0-10): ¿Se siente 2025 o 2012? Animaciones, interacciones, mobile-first. 9-10 última generación. 6-8 cumple estándar actual. 3-5 no la tocan hace años. 1-2 parece de los 2000s.

3. **claridad_propuesta** (0-10): ¿Se entiende en 5 segundos qué venden, a quién y por qué? 9-10 cristalina. 6-8 se entiende genérica. 3-5 confusa. 1-2 no se entiende.

4. **credibilidad** (0-10): Testimonios, casos, clientes, equipo con cara, números concretos. 9-10 muchas pruebas con cifras. 6-8 algo débil. 3-5 casi nada. 1-2 cero prueba.

5. **llamado_a_accion** (0-10): Botones claros, visibles, múltiples. 9-10 CTA clarísimo con incentivo. 6-8 débil o escondido. 3-5 solo un mail. 1-2 no hay forma clara de contactar.

6. **contenido_relevante** (0-10): ¿Habla del cliente o solo de sí mismos? 9-10 útil, específico. 6-8 mezcla. 3-5 todo "nosotros somos". 1-2 texto genérico de plantilla.

7. **encontrabilidad** (0-10): Título y descripción en Google, palabras clave naturales. USAR EL SCORE DE SEO DE GOOGLE como guía. 9-10 titles/descriptions potentes. 6-8 ok mejorable. 3-5 genéricos. 1-2 vacíos o pésimos.

8. **velocidad_tecnica** (0-10): Qué tan rápida y bien construida está. USAR EL SCORE DE PERFORMANCE DE GOOGLE y el tiempo de carga medido. Si carga en <2s y Google le da 85+, es 9-10. Si tarda 5s+ y Google le da <50, es 2-3.

═══════════════════════════════════════════════════════════
SCORE FINAL
═══════════════════════════════════════════════════════════

PROMEDIO simple de las 8 dimensiones, redondeado. NO SUAVICES. Si da 2.6 → score 3. Si da 7.4 → 7.

═══════════════════════════════════════════════════════════
PROBLEMAS A REPORTAR
═══════════════════════════════════════════════════════════

Los 4 más graves (4 dimensiones más bajas).

REGLA CRÍTICA: SÉ CONCISO. Nada de choclos largos. El dueño está escaneando, no leyendo un ensayo.

Para cada uno:
- titulo: en lenguaje de dueño, máx 10 palabras ("La web no transmite confianza a primera vista")
- que_pasa: **MÁX 35 palabras**. Qué detectaste CONCRETAMENTE, con 1 ejemplo puntual de lo que ves.
- que_te_cuesta: **MÁX 30 palabras**. Impacto en plata, clientes o tiempo. Directo y específico.
- por_que_no_lo_resolves_solo: **MÁX 30 palabras**. Por qué necesita un profesional. Sin vender, sin lista de tareas.

TOTAL por problema: NO MÁS de 100 palabras sumando los 3 bloques. Si te pasás, cortá.

También:
- resumen_cabecera: **MÁX 35 palabras** (2 frases cortas).
- cierre: **MÁX 60 palabras**. Invitación a una charla, sin vender agresivo. Sin listas.

═══════════════════════════════════════════════════════════
FORMATO DE SALIDA (JSON estricto, nada más)
═══════════════════════════════════════════════════════════

{{
  "dimensiones": {{
    "diseno_visual": <0-10>,
    "experiencia_moderna": <0-10>,
    "claridad_propuesta": <0-10>,
    "credibilidad": <0-10>,
    "llamado_a_accion": <0-10>,
    "contenido_relevante": <0-10>,
    "encontrabilidad": <0-10>,
    "velocidad_tecnica": <0-10>
  }},
  "score": <promedio redondeado, 0-10>,
  "resumen_cabecera": "<2 frases honestas, en lenguaje de dueño>",
  "problemas": [
    {{
      "titulo": "...",
      "que_pasa": "...",
      "que_te_cuesta": "...",
      "por_que_no_lo_resolves_solo": "..."
    }}
  ],
  "cierre": "<1 párrafo final, invitando a una charla sin vender agresivo>"
}}
"""

# ─────────────────────────────────────────────────────────────
# Scraping de texto
# ─────────────────────────────────────────────────────────────
def scrape_sitio(url: str) -> str:
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        }
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        title = soup.title.string.strip() if soup.title and soup.title.string else "(sin título)"
        meta_desc_tag = soup.find("meta", attrs={"name": "description"})
        meta_desc = meta_desc_tag["content"].strip() if meta_desc_tag and meta_desc_tag.get("content") else "(sin descripción)"

        h1s = [h.get_text(strip=True) for h in soup.find_all("h1") if h.get_text(strip=True)]
        h2s = [h.get_text(strip=True) for h in soup.find_all("h2") if h.get_text(strip=True)]

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        texto = soup.get_text(separator=" ", strip=True)
        texto = " ".join(texto.split())[:5000]

        return (
            f"TÍTULO: {title}\n"
            f"DESCRIPCIÓN: {meta_desc}\n"
            f"H1: {' | '.join(h1s) if h1s else '(ninguno)'}\n"
            f"H2: {' | '.join(h2s[:10]) if h2s else '(ninguno)'}\n\n"
            f"TEXTO VISIBLE:\n{texto}"
        )
    except Exception as e:
        return f"(ERROR scrapeando: {e})"


# ─────────────────────────────────────────────────────────────
# Playwright: browser real → screenshots + métricas
# ─────────────────────────────────────────────────────────────
def capturar_web_sync(url: str) -> dict:
    resultado = {
        "screenshots": [],
        "load_time_ms": None,
        "mobile_friendly": None,
        "broken_images": 0,
        "total_images": 0,
        "has_animations": False,
        "has_forms": False,
        "has_videos": False,
        "fonts": [],
        # Profundidad técnica oculta
        "is_https": None,
        "title": None,
        "title_length": 0,
        "meta_description": None,
        "meta_description_length": 0,
        "has_meta_pixel": False,
        "has_google_analytics": False,
        "has_gtm": False,
        "has_tiktok_pixel": False,
        "has_hotjar": False,
        "has_schema_markup": False,
        "schema_types": [],
        "has_og_tags": False,
        "og_tags_found": [],
        "has_favicon": False,
        "has_canonical": False,
        "has_robots_meta": False,
        "viewport_meta": None,
        "lang_attribute": None,
        "external_scripts_count": 0,
        "bloqueado": False,
        "html_length": 0,
        "body_text_length": 0,
        "missing_weight": 0,
        "final_url": None,
        "error": None,
    }
    try:
        with sync_playwright() as p:
            # Args para parecer un navegador real, no un bot
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            # Desktop — UA completo y headers de navegador real
            context = browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="es-AR",
                timezone_id="America/Argentina/Buenos_Aires",
                extra_http_headers={
                    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                },
            )
            # Sacamos la huella típica de navegador automatizado
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            start = time.time()
            # domcontentloaded es mucho más robusto que networkidle en sitios pesados
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Esperamos a que carguen scripts/tracking que se inyectan post-DOM
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            time.sleep(2)
            resultado["load_time_ms"] = int((time.time() - start) * 1000)
            resultado["final_url"] = page.url

            # Screenshots desktop
            resultado["screenshots"].append(("desktop_top", page.screenshot(full_page=False)))
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            time.sleep(0.8)
            resultado["screenshots"].append(("desktop_middle", page.screenshot(full_page=False)))
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.8)
            resultado["screenshots"].append(("desktop_bottom", page.screenshot(full_page=False)))

            # Métricas
            metrics = page.evaluate("""() => {
                const imgs = document.querySelectorAll('img');
                const broken = [...imgs].filter(i => !i.complete || i.naturalWidth === 0).length;
                const fonts = [...new Set([...document.querySelectorAll('h1, h2, p, body')].map(el => getComputedStyle(el).fontFamily))].slice(0, 5);
                const hasAnim = [...document.querySelectorAll('*')].slice(0, 500).some(el => {
                    const s = getComputedStyle(el);
                    return (s.animationName && s.animationName !== 'none') || (s.transitionDuration && s.transitionDuration !== '0s');
                });
                return {
                    total_images: imgs.length,
                    broken_images: broken,
                    fonts: fonts,
                    has_animations: hasAnim,
                    has_forms: document.querySelectorAll('form').length > 0,
                    has_videos: document.querySelectorAll('video, iframe[src*="youtube"], iframe[src*="vimeo"]').length > 0,
                };
            }""")
            resultado.update(metrics)

            # Profundidad técnica oculta — tracking, SEO técnico, schema, OG, etc.
            tech = page.evaluate("""() => {
                const html = document.documentElement.outerHTML;
                const scripts = [...document.querySelectorAll('script')];
                const scriptSrcs = scripts.map(s => s.src || '').join(' ');
                const scriptTexts = scripts.map(s => s.textContent || '').join(' ').slice(0, 50000);
                const allScripts = scriptSrcs + ' ' + scriptTexts;

                // Tracking — busca por src de script o por código inline
                const hasMetaPixel = /connect\\.facebook\\.net\\/.+\\/fbevents\\.js|fbq\\s*\\(\\s*['\"]init['\"]|fbq\\s*\\(\\s*['\"]track['\"]/i.test(allScripts);
                const hasGTM = /googletagmanager\\.com\\/gtm\\.js|GTM-[A-Z0-9]+/i.test(allScripts);
                const hasGA = /google-analytics\\.com\\/analytics\\.js|googletagmanager\\.com\\/gtag\\/js|gtag\\s*\\(\\s*['\"]config['\"]|UA-\\d+-\\d+|G-[A-Z0-9]+/i.test(allScripts);
                const hasTikTok = /analytics\\.tiktok\\.com|ttq\\.load|ttq\\.track/i.test(allScripts);
                const hasHotjar = /static\\.hotjar\\.com|hjid|hj\\(['\"]event['\"]/i.test(allScripts);

                // Schema markup (JSON-LD)
                const ldjsonScripts = [...document.querySelectorAll('script[type=\"application/ld+json\"]')];
                const schemaTypes = [];
                for (const s of ldjsonScripts) {
                    try {
                        const data = JSON.parse(s.textContent);
                        const items = Array.isArray(data) ? data : [data];
                        for (const item of items) {
                            if (item && item['@type']) {
                                const t = Array.isArray(item['@type']) ? item['@type'].join(',') : item['@type'];
                                schemaTypes.push(t);
                            }
                        }
                    } catch (e) {}
                }

                // Open Graph
                const ogTags = [...document.querySelectorAll('meta[property^=\"og:\"]')].map(m => m.getAttribute('property'));

                // SEO técnico básico
                const titleEl = document.querySelector('title');
                const title = titleEl ? titleEl.textContent.trim() : '';
                const descEl = document.querySelector('meta[name=\"description\"]');
                const desc = descEl ? (descEl.getAttribute('content') || '').trim() : '';
                const canonical = document.querySelector('link[rel=\"canonical\"]') !== null;
                const robots = document.querySelector('meta[name=\"robots\"]');
                const favicon = document.querySelector('link[rel*=\"icon\"]') !== null;
                const viewportEl = document.querySelector('meta[name=\"viewport\"]');
                const lang = document.documentElement.getAttribute('lang') || '';

                return {
                    is_https: window.location.protocol === 'https:',
                    title: title,
                    title_length: title.length,
                    meta_description: desc,
                    meta_description_length: desc.length,
                    has_meta_pixel: hasMetaPixel,
                    has_google_analytics: hasGA,
                    has_gtm: hasGTM,
                    has_tiktok_pixel: hasTikTok,
                    has_hotjar: hasHotjar,
                    has_schema_markup: schemaTypes.length > 0,
                    schema_types: [...new Set(schemaTypes)].slice(0, 10),
                    has_og_tags: ogTags.length > 0,
                    og_tags_found: ogTags.slice(0, 10),
                    has_favicon: favicon,
                    has_canonical: canonical,
                    has_robots_meta: robots !== null,
                    viewport_meta: viewportEl ? viewportEl.getAttribute('content') : null,
                    lang_attribute: lang,
                    external_scripts_count: scripts.filter(s => s.src && !s.src.startsWith(window.location.origin)).length,
                };
            }""")
            resultado.update(tech)

            # ─── Detección de bloqueo / bot detection ───
            # Tres caminos: (1) título de challenge page, (2) HTTPS inconsistente,
            # (3) score ponderado de "señales básicas faltantes" — una web real, aún
            # mala, tiene la mayoría de estas cosas básicas.
            try:
                html_len = page.evaluate("document.documentElement.outerHTML.length")
            except Exception:
                html_len = 0
            resultado["html_length"] = html_len

            try:
                body_text_len = page.evaluate("(document.body && document.body.innerText || '').length")
            except Exception:
                body_text_len = 0
            resultado["body_text_length"] = body_text_len

            # 1) Título característico de Cloudflare / WAF / challenge pages
            title_lower = (resultado.get("title") or "").lower()
            challenge_markers = [
                "just a moment",
                "attention required",
                "access denied",
                "checking your browser",
                "cloudflare",
                "ddos protection",
                "verifying you are human",
                "verifica que eres humano",
                "verificación de seguridad",
                "un momento",
                "please wait",
                "403 forbidden",
                "bot detection",
            ]
            is_challenge = bool(title_lower) and any(m in title_lower for m in challenge_markers)

            # 2) Inconsistencia HTTPS — pedimos https:// pero el navegador no llegó
            url_pedida_https = url.lower().startswith("https://")
            https_inconsistente = url_pedida_https and resultado.get("is_https") is False

            # 3) Score ponderado de señales básicas faltantes
            # Pesos: cuanto más raro es que falte, más pesa.
            missing_weight = 0
            if not resultado.get("title"):
                missing_weight += 3  # casi ninguna web real no tiene título
            if resultado.get("external_scripts_count", 0) == 0:
                missing_weight += 3  # sin scripts externos es muy raro
            if not resultado.get("has_favicon"):
                missing_weight += 1
            if not resultado.get("lang_attribute"):
                missing_weight += 1
            if not resultado.get("viewport_meta"):
                missing_weight += 1
            if not resultado.get("meta_description"):
                missing_weight += 1
            if not resultado.get("has_og_tags"):
                missing_weight += 1
            tracking_count = sum([
                bool(resultado.get("has_meta_pixel")),
                bool(resultado.get("has_google_analytics")),
                bool(resultado.get("has_gtm")),
                bool(resultado.get("has_tiktok_pixel")),
                bool(resultado.get("has_hotjar")),
            ])
            if tracking_count == 0:
                missing_weight += 1
            if html_len < 8000:
                missing_weight += 2  # contenido sospechosamente chico
            if body_text_len < 200:
                missing_weight += 2  # body casi vacío = probable challenge / shell

            resultado["missing_weight"] = missing_weight

            # Decisión final
            if is_challenge or https_inconsistente or missing_weight >= 7:
                resultado["bloqueado"] = True

            # Mobile — mismo UA y configuración
            context_mobile = browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                locale="es-AR",
                extra_http_headers={"Accept-Language": "es-AR,es;q=0.9,en;q=0.8"},
            )
            page_m = context_mobile.new_page()
            try:
                page_m.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page_m.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                time.sleep(1)
                resultado["screenshots"].append(("mobile", page_m.screenshot(full_page=False)))
                has_h_scroll = page_m.evaluate("document.documentElement.scrollWidth > window.innerWidth + 5")
                resultado["mobile_friendly"] = not has_h_scroll
            except Exception:
                resultado["mobile_friendly"] = False

            browser.close()
    except Exception as e:
        resultado["error"] = str(e)
        # Si Playwright explotó antes de capturar nada, casi seguro fue bloqueo
        # (timeout por challenge de Cloudflare, 403, navigation aborted, etc).
        # Marcamos bloqueado para mostrar el cartel honesto en vez de un listado
        # rojo engañoso.
        resultado["bloqueado"] = True
    return resultado


# ─────────────────────────────────────────────────────────────
# Google PageSpeed Insights (con API key opcional para subir el rate limit)
# ─────────────────────────────────────────────────────────────
def obtener_pagespeed_sync(url: str) -> dict:
    api = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    resultado = {
        "performance": None, "accessibility": None,
        "best_practices": None, "seo": None, "error": None,
    }
    try:
        params = [
            ("url", url),
            ("category", "performance"),
            ("category", "accessibility"),
            ("category", "best-practices"),
            ("category", "seo"),
            ("strategy", "desktop"),
        ]
        api_key = os.getenv("PAGESPEED_API_KEY", "").strip()
        if api_key:
            params.append(("key", api_key))
        r = requests.get(api, params=params, timeout=60)
        if r.status_code == 200:
            data = r.json()
            cats = data.get("lighthouseResult", {}).get("categories", {})
            for k_in, k_out in [("performance", "performance"), ("accessibility", "accessibility"),
                                ("best-practices", "best_practices"), ("seo", "seo")]:
                score = cats.get(k_in, {}).get("score")
                if score is not None:
                    resultado[k_out] = int(score * 100)
        else:
            resultado["error"] = f"status {r.status_code}"
    except Exception as e:
        resultado["error"] = str(e)
    return resultado


# ─────────────────────────────────────────────────────────────
# Wayback Machine — fallback cuando Cloudflare/WAF bloquea Playwright
# Tomamos el HTML cacheado por archive.org y lo analizamos como si fuera
# nuestra propia captura.
# ─────────────────────────────────────────────────────────────
def obtener_html_wayback(url: str) -> str | None:
    """Devuelve el HTML del snapshot más reciente de archive.org, o None."""
    try:
        check = requests.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            timeout=10,
        )
        data = check.json()
        snap = data.get("archived_snapshots", {}).get("closest", {})
        if not snap.get("available"):
            return None
        timestamp = snap.get("timestamp", "")
        if not timestamp:
            return None
        # El sufijo "id_" después del timestamp devuelve el HTML original
        # sin la barra ni los inserts de Wayback.
        raw_url = f"https://web.archive.org/web/{timestamp}id_/{url}"
        resp = requests.get(
            raw_url,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        )
        if resp.status_code == 200 and resp.text:
            return resp.text
    except Exception:
        pass
    return None


def analizar_html_estatico(html: str, url: str) -> dict:
    """
    Aplica el mismo análisis que Playwright sobre HTML estático
    (de Wayback o cualquier fuente). Devuelve el mismo shape de campos.
    """
    out = {
        "title": None, "title_length": 0,
        "meta_description": None, "meta_description_length": 0,
        "is_https": url.lower().startswith("https://"),
        "has_canonical": False, "has_robots_meta": False,
        "lang_attribute": None, "viewport_meta": None, "has_favicon": False,
        "has_meta_pixel": False, "has_google_analytics": False,
        "has_gtm": False, "has_tiktok_pixel": False, "has_hotjar": False,
        "has_og_tags": False, "og_tags_found": [],
        "has_schema_markup": False, "schema_types": [],
        "external_scripts_count": 0,
    }
    try:
        soup = BeautifulSoup(html, "html.parser")
        # Title
        if soup.title and soup.title.string:
            t = soup.title.string.strip()
            out["title"] = t[:300]
            out["title_length"] = len(t)
        # Meta description
        md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        if md and md.get("content"):
            d = md["content"].strip()
            out["meta_description"] = d[:500]
            out["meta_description_length"] = len(d)
        # Canonical
        out["has_canonical"] = bool(soup.find("link", attrs={"rel": re.compile(r"canonical", re.I)}))
        # Robots
        out["has_robots_meta"] = bool(soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)}))
        # Lang
        if soup.html and soup.html.get("lang"):
            out["lang_attribute"] = soup.html.get("lang")
        # Viewport
        vp = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
        if vp and vp.get("content"):
            out["viewport_meta"] = vp["content"]
        # Favicon
        out["has_favicon"] = bool(soup.find("link", attrs={"rel": re.compile(r"icon", re.I)}))
        # OG tags
        og = [m.get("property") for m in soup.find_all("meta") if m.get("property", "").lower().startswith("og:")]
        og = [x for x in og if x]
        out["og_tags_found"] = og[:10]
        out["has_og_tags"] = bool(og)
        # Schema markup (JSON-LD)
        types = []
        for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(sc.string or "")
                items = payload if isinstance(payload, list) else [payload]
                for it in items:
                    if isinstance(it, dict) and "@type" in it:
                        v = it["@type"]
                        if isinstance(v, list):
                            types.extend(str(x) for x in v)
                        else:
                            types.append(str(v))
            except Exception:
                pass
        out["schema_types"] = list(dict.fromkeys(types))[:10]
        out["has_schema_markup"] = bool(types)
        # Tracking — buscamos tanto en src de scripts como en código inline
        scripts_html = " ".join(
            [(s.get("src") or "") + " " + (s.string or "") for s in soup.find_all("script")]
        )
        out["has_meta_pixel"] = bool(re.search(r"fbevents\.js|fbq\s*\(\s*['\"]init|fbq\s*\(\s*['\"]track", scripts_html, re.I))
        out["has_gtm"] = bool(re.search(r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]+", scripts_html))
        out["has_google_analytics"] = bool(re.search(r"google-analytics\.com/analytics\.js|googletagmanager\.com/gtag/js|UA-\d+-\d+|G-[A-Z0-9]+", scripts_html))
        out["has_tiktok_pixel"] = bool(re.search(r"analytics\.tiktok\.com|ttq\.load|ttq\.track", scripts_html, re.I))
        out["has_hotjar"] = bool(re.search(r"static\.hotjar\.com|hjid", scripts_html, re.I))
        # External scripts count
        external = 0
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower()
            for s in soup.find_all("script"):
                src = s.get("src") or ""
                if src and "://" in src and host not in src.lower():
                    external += 1
        except Exception:
            pass
        out["external_scripts_count"] = external
    except Exception as e:
        out["parse_error"] = str(e)
    return out


# ─────────────────────────────────────────────────────────────
# Rutas
# ─────────────────────────────────────────────────────────────
# Nota: el "/" ya está definido arriba como health check.
# La UI vive en la web Next.js (resulto.com.ar/auditoria).


# ─────────────────────────────────────────────────────────────
# Helper: análisis completo de una URL (reutilizable para usuario y competidor)
# ─────────────────────────────────────────────────────────────
async def analizar_url_full(
    url: str, rubro: str, tiempo: str, publicidad: str, objetivo: str,
    contexto_extra: str = "",
) -> dict:
    """
    Corre el pipeline completo (scrape + Playwright + PageSpeed + Gemini)
    sobre una URL y devuelve el dict de resultado (sin guardar en DB).
    """
    contenido = await asyncio.to_thread(scrape_sitio, url)
    captura = await asyncio.to_thread(capturar_web_sync, url)
    pagespeed = await asyncio.to_thread(obtener_pagespeed_sync, url)

    # ─── Fallback cuando Playwright bloqueó: pedir HTML a Wayback Machine ───
    # archive.org no le importa Cloudflare. Si tiene un snapshot, parseamos
    # ese HTML y mergeamos los datos sobre la captura.
    fuente_datos = "playwright"
    if captura.get("bloqueado"):
        html_archivado = await asyncio.to_thread(obtener_html_wayback, url)
        if html_archivado:
            datos_alt = analizar_html_estatico(html_archivado, url)
            for k, v in datos_alt.items():
                # Solo pisamos si en la captura ese campo es vacío/falso
                cur = captura.get(k)
                if cur in (None, "", 0, False, []):
                    captura[k] = v
            # Si recuperamos title y al menos algún dato real, ya no está
            # "ciego": destrabamos para que el modelo y el frontend muestren
            # data real con disclaimer de fuente.
            if datos_alt.get("title") or datos_alt.get("has_og_tags") or datos_alt.get("external_scripts_count", 0) > 0:
                captura["bloqueado"] = False
                captura["fuente"] = "wayback"
                fuente_datos = "wayback"

    # Si Playwright se rompió Y Wayback tampoco trajo nada, marcamos
    # "ciego" para no llamar a Gemini con data fantasma. La respuesta final
    # va a indicar que no se pudo auditar.
    sin_data_real = (
        captura.get("bloqueado")
        and not captura.get("title")
        and (captura.get("external_scripts_count", 0) or 0) == 0
        and not captura.get("has_og_tags")
    )
    if sin_data_real:
        return {
            "no_se_pudo_auditar": True,
            "url_analizada": url,
            "rubro_analizado": rubro,
            "razon": (
                "Este sitio bloqueó nuestro análisis automático (suele pasar con webs "
                "protegidas por Cloudflare o WAFs). Tampoco encontramos un snapshot "
                "reciente en archive.org. No vamos a inventar un puntaje sobre datos "
                "que no pudimos leer."
            ),
            "pagespeed": pagespeed,
            "setup_tecnico": {
                "bloqueado": True,
                "fuente": None,
                "tracking": {"meta_pixel": None, "google_analytics": None, "google_tag_manager": None, "tiktok_pixel": None, "hotjar": None},
                "seo": {"https": None, "title": None, "title_length": 0, "meta_description": None, "meta_description_length": 0, "canonical": None, "robots_meta": None, "lang": None, "favicon": None},
                "social_y_datos": {"open_graph": None, "og_tags": [], "schema_markup": None, "schema_types": []},
            },
        }

    extras = f"""
{contexto_extra}
DATOS OBJETIVOS MEDIDOS POR UN NAVEGADOR REAL:
- Tiempo de carga: {captura.get('load_time_ms')} ms
- Mobile-friendly (sin scroll horizontal en celu): {captura.get('mobile_friendly')}
- Imágenes rotas: {captura.get('broken_images')} de {captura.get('total_images')}
- Tiene animaciones CSS/JS: {captura.get('has_animations')}
- Tiene formularios: {captura.get('has_forms')}
- Tiene videos: {captura.get('has_videos')}
- Fuentes detectadas: {captura.get('fonts')}

SCORES OFICIALES DE GOOGLE PAGESPEED (0-100):
- Performance (velocidad): {pagespeed.get('performance')}
- Accesibilidad: {pagespeed.get('accessibility')}
- Buenas prácticas: {pagespeed.get('best_practices')}
- SEO: {pagespeed.get('seo')}

PROFUNDIDAD TÉCNICA OCULTA (lo que el dueño no ve a simple vista):
Tracking & medición:
- Pixel de Meta (Facebook/Instagram Ads) instalado: {captura.get('has_meta_pixel')}
- Google Analytics instalado: {captura.get('has_google_analytics')}
- Google Tag Manager instalado: {captura.get('has_gtm')}
- TikTok Pixel: {captura.get('has_tiktok_pixel')}
- Hotjar (mapas de calor): {captura.get('has_hotjar')}

SEO técnico:
- HTTPS (sitio seguro): {captura.get('is_https')}
- Title (largo en chars, ideal 50-60): "{captura.get('title')}" ({captura.get('title_length')} chars)
- Meta description (largo en chars, ideal 140-160): "{captura.get('meta_description')}" ({captura.get('meta_description_length')} chars)
- Tag canonical (evita contenido duplicado): {captura.get('has_canonical')}
- Meta robots (control indexación): {captura.get('has_robots_meta')}
- Atributo lang en HTML: {captura.get('lang_attribute') or '(sin lang)'}
- Viewport meta (responsive declarado): {captura.get('viewport_meta')}
- Favicon: {captura.get('has_favicon')}

Datos estructurados & redes sociales:
- Schema markup (JSON-LD para Google): {captura.get('has_schema_markup')} — tipos: {captura.get('schema_types')}
- Open Graph (preview rico al compartir en WhatsApp/Facebook): {captura.get('has_og_tags')} — tags: {captura.get('og_tags_found')}

INTERPRETACIÓN PARA EL DUEÑO (traducir a un lenguaje claro y profesional, sin jerga ni modismos):
- Si NO tiene Meta Pixel y SÍ hace publicidad → no puede medir si los avisos generan resultados ni volver a impactar a las personas que ya entraron a su web. La inversión publicitaria pierde efectividad.
- Si NO tiene Google Analytics ni GTM → no tiene visibilidad de qué pasa dentro de su web: cuántas personas entran, qué hacen, dónde se van.
- Si NO tiene Open Graph → cuando alguien comparte su web por WhatsApp o redes, el preview aparece sin imagen ni descripción y luce poco profesional.
- Si NO tiene Schema markup → Google muestra su resultado con menos información (sin estrellas, precios, horarios, etc.) que el de la competencia.
- Si NO es HTTPS → el navegador le advierte al visitante que el sitio no es seguro, generando desconfianza inmediata.
- Si el title está vacío, muy corto o muy largo → en Google aparece cortado o poco atractivo, bajando los clicks.
- Si la meta description está vacía → Google muestra un fragmento de texto arbitrario en lugar de un resumen pensado para vender.

TONO DE TUS RESPUESTAS (importante):
- Profesional, claro y directo. Como un consultor senior hablando con el dueño de un negocio.
- Sin modismos callejeros. No usar expresiones tipo "tirar guita", "volar a ciegas", "estás al horno", "una papa".
- Sí podés usar "vos" rioplatense (es un público argentino), pero el registro debe ser sobrio y profesional.
- Cuando algo está mal, decílo con firmeza, pero con respeto y datos.
"""

    prompt_final = PROMPT_MAESTRO.format(
        url=url, rubro=rubro, tiempo=tiempo, publicidad=publicidad,
        objetivo=objetivo, contenido=contenido, extras=extras,
    )

    partes = [prompt_final]
    for name, img_bytes in captura.get("screenshots", []):
        try:
            img = Image.open(io.BytesIO(img_bytes))
            partes.append(f"\n[Captura: {name}]")
            partes.append(img)
        except Exception:
            pass

    response = await asyncio.to_thread(model.generate_content, partes)
    data = json.loads(response.text)
    data["pagespeed"] = pagespeed
    data["metricas_tecnicas"] = {
        "tiempo_carga_ms": captura.get("load_time_ms"),
        "mobile_friendly": captura.get("mobile_friendly"),
        "imagenes_rotas": captura.get("broken_images"),
        "total_imagenes": captura.get("total_images"),
    }
    data["setup_tecnico"] = {
        "bloqueado": bool(captura.get("bloqueado")),
        "fuente": captura.get("fuente") or fuente_datos,
        "final_url": captura.get("final_url"),
        "html_length": captura.get("html_length"),
        "tracking": {
            "meta_pixel": captura.get("has_meta_pixel"),
            "google_analytics": captura.get("has_google_analytics"),
            "google_tag_manager": captura.get("has_gtm"),
            "tiktok_pixel": captura.get("has_tiktok_pixel"),
            "hotjar": captura.get("has_hotjar"),
        },
        "seo": {
            "https": captura.get("is_https"),
            "title": captura.get("title"),
            "title_length": captura.get("title_length"),
            "meta_description": captura.get("meta_description"),
            "meta_description_length": captura.get("meta_description_length"),
            "canonical": captura.get("has_canonical"),
            "robots_meta": captura.get("has_robots_meta"),
            "lang": captura.get("lang_attribute"),
            "favicon": captura.get("has_favicon"),
        },
        "social_y_datos": {
            "open_graph": captura.get("has_og_tags"),
            "og_tags": captura.get("og_tags_found"),
            "schema_markup": captura.get("has_schema_markup"),
            "schema_types": captura.get("schema_types"),
        },
    }
    return data


def comparar_dimensiones(own: dict, comp: dict) -> dict:
    """
    Compara dimensiones del usuario vs el competidor.
    Devuelve listas de dónde el usuario gana, pierde o empata,
    y un diff numérico por dimensión.
    """
    own_dims = own.get("dimensiones", {}) or {}
    comp_dims = comp.get("dimensiones", {}) or {}
    ganadas, perdidas, empates = [], [], []
    diff_por_dim = {}
    for k in own_dims.keys():
        a = int(own_dims.get(k, 0) or 0)
        b = int(comp_dims.get(k, 0) or 0)
        diff_por_dim[k] = a - b
        if a > b:
            ganadas.append(k)
        elif a < b:
            perdidas.append(k)
        else:
            empates.append(k)

    own_score = float(own.get("score", 0) or 0)
    comp_score = float(comp.get("score", 0) or 0)
    if own_score > comp_score:
        veredicto = "ganas"
    elif own_score < comp_score:
        veredicto = "pierdes"
    else:
        veredicto = "empate"

    return {
        "ganadas": ganadas,
        "perdidas": perdidas,
        "empates": empates,
        "diff_por_dim": diff_por_dim,
        "score_usuario": own_score,
        "score_competidor": comp_score,
        "diff_score": round(own_score - comp_score, 1),
        "veredicto_general": veredicto,
    }


@app.post("/diagnosticar")
async def diagnosticar(
    email: str = Form(...),
    url: str = Form(...),
    rubro: str = Form(...),
    tiempo: str = Form(...),
    publicidad: str = Form(...),
    objetivo: str = Form(...),
    url_competidor: str = Form(""),
):
    comp_url = (url_competidor or "").strip()
    tiene_competidor = bool(comp_url)

    async def stream():
        def event(progress, message, **extra):
            return json.dumps({"progress": progress, "message": message, **extra}) + "\n"

        try:
            yield event(5, "Iniciando análisis...")

            # ── Caché para la web del usuario (si NO hay competidor) ──
            cached = None
            if not tiene_competidor:
                cached = await asyncio.to_thread(buscar_auditoria_reciente, url)
                if cached:
                    yield event(40, "Encontramos una auditoría reciente de tu web...")
                    try:
                        share_id = await asyncio.to_thread(
                            guardar_auditoria,
                            email, url, rubro, tiempo, publicidad, objetivo, cached,
                        )
                        cached["share_id"] = share_id
                    except Exception as e:
                        print(f"[WARN] No pude guardar el lead cacheado: {e}")
                    yield event(100, "¡Listo!", done=True, result=cached)
                    return

            # ── Análisis del usuario (y competidor en paralelo si corresponde) ──
            if tiene_competidor:
                yield event(15, "Analizando tu web y la del competidor en paralelo...")
            else:
                yield event(15, "Analizando tu web...")

            tareas = [analizar_url_full(url, rubro, tiempo, publicidad, objetivo)]
            if tiene_competidor:
                contexto_comp = (
                    "NOTA: Estás analizando el sitio web de un COMPETIDOR del cliente "
                    f"(rubro '{rubro}'). Mantené los mismos criterios y exigencia de scoring "
                    "que para cualquier otra web. No suavices ni infles. Tu trabajo es "
                    "evaluar el sitio competidor con la misma vara que cualquier otro."
                )
                tareas.append(
                    analizar_url_full(
                        comp_url, rubro, tiempo, publicidad, objetivo,
                        contexto_extra=contexto_comp,
                    )
                )

            yield event(35, "Capturando, midiendo velocidad y comparando con IA...")
            resultados = await asyncio.gather(*tareas)
            data_final = resultados[0]
            comp_data = resultados[1] if tiene_competidor else None

            yield event(85, "Procesando resultados...")

            # Si hay competidor, lo adjuntamos + comparativa honesta dimensión por dimensión
            if comp_data is not None:
                data_final["competidor"] = {
                    "url": comp_url,
                    "score": comp_data.get("score"),
                    "dimensiones": comp_data.get("dimensiones", {}),
                    "setup_tecnico": comp_data.get("setup_tecnico", {}),
                    "resumen_cabecera": comp_data.get("resumen_cabecera", ""),
                    "pagespeed": comp_data.get("pagespeed", {}),
                }
                data_final["comparacion"] = comparar_dimensiones(data_final, comp_data)

            # Guardar el lead + auditoría en la DB y devolver share_id
            yield event(95, "Guardando tu auditoría...")
            # Solo cacheamos auditorías "limpias" (sin competidor).
            if not tiene_competidor:
                data_final["__cache_url_norm"] = normalizar_url(url)
            data_final["url_analizada"] = url
            try:
                share_id = await asyncio.to_thread(
                    guardar_auditoria,
                    email, url, rubro, tiempo, publicidad, objetivo, data_final,
                )
                data_final["share_id"] = share_id
            except Exception as e:
                # Si falla el guardado no rompe la respuesta — el usuario igual ve el resultado
                print(f"[WARN] No pude guardar la auditoría: {e}")

            yield event(100, "¡Listo!", done=True, result=data_final)

        except Exception as e:
            yield event(100, f"Error: {e}", done=True, error=str(e))

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ─────────────────────────────────────────────────────────────
# Auditorías guardadas (para el link de compartir)
# ─────────────────────────────────────────────────────────────
@app.get("/api/auditoria/{share_id}")
def obtener_auditoria(share_id: str):
    """Devuelve una auditoría guardada por su share_id (para el link de compartir)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "SELECT resultado_json, url, rubro, created_at FROM auditorias WHERE share_id = ?",
            (share_id,),
        )
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Auditoría no encontrada")
    resultado = json.loads(row[0])
    resultado["url_analizada"] = row[1]
    resultado["rubro_analizado"] = row[2]
    resultado["fecha_creada"] = row[3]
    resultado["share_id"] = share_id
    return JSONResponse(resultado)


# ─────────────────────────────────────────────────────────────
# PDF descargable de la auditoría
# ─────────────────────────────────────────────────────────────
def _html_escape(s) -> str:
    """Escape HTML básico."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _highlight_pdf(text: str) -> str:
    """Resalta números seguidos de % o $ con un color verde sobrio en el PDF."""
    if not text:
        return ""
    import re
    s = _html_escape(text)
    # números con % o $ → verdes y bold
    s = re.sub(r"(\d+(?:[.,]\d+)?\s*%)", r'<span class="hl-num">\1</span>', s)
    s = re.sub(r"(\$\s*\d+(?:[.,]\d+)?)", r'<span class="hl-num">\1</span>', s)
    return s


DIMENSIONES_LABEL = [
    ("diseno_visual", "Diseño visual"),
    ("experiencia_moderna", "Experiencia moderna"),
    ("claridad_propuesta", "Claridad de propuesta"),
    ("credibilidad", "Credibilidad"),
    ("llamado_a_accion", "Llamado a la acción"),
    ("contenido_relevante", "Contenido relevante"),
    ("encontrabilidad", "Encontrabilidad (SEO)"),
    ("velocidad_tecnica", "Velocidad técnica"),
]


def _color_score(v: int) -> str:
    if v >= 8:
        return "#16a34a"
    if v >= 6:
        return "#65a30d"
    if v >= 4:
        return "#eab308"
    if v >= 2:
        return "#f97316"
    return "#dc2626"


def generar_html_pdf(data: dict, url_analizada: str, fecha: str) -> str:
    """Construye el HTML que después se renderiza como PDF."""
    score = int(data.get("score", 0) or 0)
    color_score = _color_score(score)
    resumen = _highlight_pdf(data.get("resumen_cabecera", ""))
    cierre = _highlight_pdf(data.get("cierre", ""))
    dims = data.get("dimensiones", {}) or {}

    # Dimensiones en grid 2x4
    dims_html = ""
    for key, label in DIMENSIONES_LABEL:
        v = int(dims.get(key, 0) or 0)
        c = _color_score(v)
        pct = (v / 10) * 100
        dims_html += f"""
        <div class="dim-card">
          <div class="dim-label">{_html_escape(label)}</div>
          <div class="dim-row">
            <div class="dim-num"><span style="color:{c}">{v}</span><span class="dim-num-sub">/10</span></div>
            <div class="dim-bar"><div class="dim-bar-fill" style="width:{pct}%;background:{c}"></div></div>
          </div>
        </div>
        """

    # Problemas
    problemas_html = ""
    for i, p in enumerate(data.get("problemas", []) or [], start=1):
        problemas_html += f"""
        <div class="problema">
          <div class="problema-head">
            <div class="problema-num">{i}</div>
            <div class="problema-titulo">{_html_escape(p.get("titulo", ""))}</div>
          </div>
          <div class="problema-block">
            <div class="problema-label">Qué pasa</div>
            <div class="problema-text">{_highlight_pdf(p.get("que_pasa", ""))}</div>
          </div>
          <div class="problema-block">
            <div class="problema-label">Qué te cuesta</div>
            <div class="problema-text">{_highlight_pdf(p.get("que_te_cuesta", ""))}</div>
          </div>
          <div class="problema-block">
            <div class="problema-label">Por qué no lo resolvés solo</div>
            <div class="problema-text">{_highlight_pdf(p.get("por_que_no_lo_resolves_solo", ""))}</div>
          </div>
        </div>
        """

    # Comparación con competidor (si existe)
    comp_html = ""
    competidor = data.get("competidor") or {}
    comparacion = data.get("comparacion") or {}
    if competidor and comparacion:
        comp_url = competidor.get("url", "")
        comp_score = float(competidor.get("score") or 0)
        own_score = float(data.get("score") or 0)
        comp_dims = competidor.get("dimensiones", {}) or {}
        diff = round(own_score - comp_score, 1)

        # Dominio corto para mostrar
        def _dom_short(u):
            from urllib.parse import urlparse
            try:
                host = urlparse(u).hostname or u
                return (host or "").replace("www.", "")
            except Exception:
                return u

        dom_user = _dom_short(url_analizada)
        dom_comp = _dom_short(comp_url)
        ganadas = len(comparacion.get("ganadas", []) or [])
        perdidas = len(comparacion.get("perdidas", []) or [])
        empates = len(comparacion.get("empates", []) or [])
        veredicto = comparacion.get("veredicto_general", "empate")

        if veredicto == "ganas":
            frase = f"Tu web está <strong>{abs(diff)} puntos arriba</strong> de {_html_escape(dom_comp)}. Ganás en {ganadas} dimensiones, perdés en {perdidas} y empatás en {empates}."
        elif veredicto == "pierdes":
            frase = f"Tu web está <strong>{abs(diff)} puntos abajo</strong> de {_html_escape(dom_comp)}. Perdés en {perdidas} dimensiones, ganás en {ganadas} y empatás en {empates}."
        else:
            frase = f"Empate técnico contra {_html_escape(dom_comp)}. Ganás en {ganadas}, perdés en {perdidas} y empatás en {empates}."

        comp_rows = ""
        for key, label in DIMENSIONES_LABEL:
            a = int(dims.get(key, 0) or 0)
            b = int(comp_dims.get(key, 0) or 0)
            if a > b: badge_cls, badge_txt = "win", "Ganás"
            elif a < b: badge_cls, badge_txt = "lose", "Perdés"
            else: badge_cls, badge_txt = "tie", "Empate"
            cu, cc = _color_score(a), _color_score(b)
            comp_rows += f"""
            <div class="comp-row">
              <div class="comp-row-name">{_html_escape(label)}</div>
              <div class="comp-row-num user" style="background:{cu}">{a}</div>
              <div class="comp-row-num" style="background:{cc}">{b}</div>
              <div class="comp-row-badge {badge_cls}">{badge_txt}</div>
            </div>
            """

        comp_html = f"""
        <div class="section-title">Comparación con competidor</div>
        <div class="comp-card">
          <div class="comp-verdict">{frase}</div>
          <div class="comp-scores">
            <div class="comp-score user-block">
              <div class="comp-score-label">Tu web</div>
              <div class="comp-score-value" style="color:{_color_score(int(own_score))}">{own_score:.1f}</div>
              <div class="comp-score-url">{_html_escape(dom_user)}</div>
            </div>
            <div class="comp-score">
              <div class="comp-score-label">Competidor</div>
              <div class="comp-score-value" style="color:{_color_score(int(comp_score))}">{comp_score:.1f}</div>
              <div class="comp-score-url">{_html_escape(dom_comp)}</div>
            </div>
          </div>
          <div class="comp-list">
            {comp_rows}
          </div>
        </div>
        """

    # Setup técnico (si existe)
    tech_html = ""
    setup = data.get("setup_tecnico") or {}
    if setup:
        if setup.get("bloqueado"):
            # La web bloqueó a nuestro analizador (común en sitios grandes con WAF / Cloudflare)
            tech_html = """
            <div class="section-title">Setup técnico</div>
            <div class="tech-blocked">
              <div class="tech-blocked-title">No pudimos analizar el setup técnico de este sitio</div>
              <div class="tech-blocked-text">
                La web bloqueó el análisis automático (suele pasar con sitios grandes que tienen
                protecciones tipo Cloudflare o WAF que filtran herramientas automatizadas).
                Esto <strong>no significa que el sitio esté mal configurado</strong> — simplemente
                no podemos leer su tracking y SEO técnico desde acá. La parte visual y la
                puntuación general sí son válidas porque las hace una IA mirando capturas reales.
              </div>
            </div>
            """
        else:
            tr = setup.get("tracking", {}) or {}
            seo = setup.get("seo", {}) or {}
            soc = setup.get("social_y_datos", {}) or {}

            def row(label, ok):
                icon = "✓" if ok else "✗"
                cls = "ok" if ok else "no"
                return f'<div class="tech-row {cls}"><span class="tech-icon">{icon}</span><span>{_html_escape(label)}</span></div>'

            tech_html = f"""
            <div class="section-title">Setup técnico</div>
            <div class="tech-grid">
              <div class="tech-group">
                <div class="tech-group-title">Medición & tracking</div>
                {row("Pixel de Meta", tr.get("meta_pixel"))}
                {row("Google Analytics", tr.get("google_analytics"))}
                {row("Google Tag Manager", tr.get("google_tag_manager"))}
                {row("TikTok Pixel", tr.get("tiktok_pixel"))}
                {row("Hotjar / mapas de calor", tr.get("hotjar"))}
              </div>
              <div class="tech-group">
                <div class="tech-group-title">SEO técnico</div>
                {row("HTTPS (sitio seguro)", seo.get("https"))}
                {row("Title de la página", bool(seo.get("title")))}
                {row("Meta description", bool(seo.get("meta_description")))}
                {row("Tag canonical", seo.get("canonical"))}
                {row("Atributo lang en HTML", bool(seo.get("lang")))}
                {row("Favicon", seo.get("favicon"))}
              </div>
              <div class="tech-group">
                <div class="tech-group-title">Compartir & Google</div>
                {row("Open Graph (preview en redes)", soc.get("open_graph"))}
                {row("Schema markup (datos enriquecidos)", soc.get("schema_markup"))}
              </div>
            </div>
            """

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Auditoría RESULTO — {_html_escape(url_analizada)}</title>
<style>
  @page {{ size: A4; margin: 18mm 14mm; }}
  * {{ box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    color: #0a0a0a; margin: 0; padding: 0;
    font-size: 11.5pt; line-height: 1.5;
  }}
  .header {{
    display: flex; justify-content: space-between; align-items: center;
    padding-bottom: 14px; border-bottom: 2px solid #88d500;
    margin-bottom: 22px;
  }}
  .logo {{ font-weight: 900; font-size: 20pt; letter-spacing: -0.02em; }}
  .logo .dot {{ color: #88d500; }}
  .header-meta {{ font-size: 9.5pt; color: #5b6b4a; text-align: right; }}
  .url-line {{
    background: #f4f8e8; border-left: 3px solid #88d500;
    padding: 10px 14px; border-radius: 6px;
    font-size: 10pt; word-break: break-all;
    margin-bottom: 18px;
  }}
  .url-line strong {{ color: #4d7a00; }}

  .score-section {{
    display: flex; gap: 22px; align-items: stretch;
    margin-bottom: 26px; page-break-inside: avoid;
  }}
  .score-box {{
    width: 140px; flex-shrink: 0;
    background: #0a0a0a; color: #fff;
    border-radius: 14px; padding: 16px;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    text-align: center;
  }}
  .score-num {{ font-size: 52pt; font-weight: 900; line-height: 1; color: {color_score}; }}
  .score-num-sub {{ font-size: 12pt; color: #999; margin-top: 4px; }}
  .score-label {{ font-size: 9pt; text-transform: uppercase; letter-spacing: 0.1em; color: #ccc; margin-top: 8px; }}
  .resumen {{
    flex: 1;
    background: #fafbf6; border: 1px solid #eef0e8;
    border-radius: 14px; padding: 18px;
    font-size: 11.5pt; line-height: 1.55; color: #1a1a1a;
  }}
  .hl-num {{ color: #4d7a00; font-weight: 700; }}

  .section-title {{
    font-size: 13pt; font-weight: 800; letter-spacing: -0.01em;
    margin: 28px 0 14px; padding-bottom: 6px;
    border-bottom: 1px solid #e2e8d4;
  }}

  /* Dimensiones */
  .dims-grid {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
    page-break-inside: avoid;
  }}
  .dim-card {{
    background: #fafbf6; border: 1px solid #eef0e8;
    border-radius: 10px; padding: 10px 14px;
  }}
  .dim-label {{ font-size: 9.5pt; color: #5b6b4a; margin-bottom: 4px; font-weight: 600; }}
  .dim-row {{ display: flex; align-items: center; gap: 12px; }}
  .dim-num {{ font-size: 18pt; font-weight: 900; line-height: 1; min-width: 50px; }}
  .dim-num-sub {{ font-size: 10pt; color: #888; font-weight: 600; }}
  .dim-bar {{ flex: 1; height: 6px; background: #eef0e8; border-radius: 999px; overflow: hidden; }}
  .dim-bar-fill {{ height: 100%; border-radius: 999px; }}

  /* Problemas */
  .problema {{
    border: 1px solid #eef0e8; border-radius: 12px;
    padding: 14px 16px; margin-bottom: 12px;
    page-break-inside: avoid;
    background: #fff;
  }}
  .problema-head {{
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 10px;
    padding-bottom: 8px; border-bottom: 1px solid #f0f2e8;
  }}
  .problema-num {{
    width: 24px; height: 24px;
    background: #88d500; color: #0a0a0a;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 11pt;
    flex-shrink: 0;
  }}
  .problema-titulo {{ font-size: 11.5pt; font-weight: 700; line-height: 1.3; }}
  .problema-block {{ margin-top: 8px; }}
  .problema-label {{
    font-size: 8.5pt; text-transform: uppercase; letter-spacing: 0.07em;
    color: #5b6b4a; font-weight: 700; margin-bottom: 2px;
  }}
  .problema-text {{ font-size: 10.5pt; line-height: 1.5; color: #1a1a1a; }}

  /* Setup técnico */
  .tech-blocked {{
    border: 1px solid #f0e6c8; background: #fffbef;
    border-radius: 12px; padding: 14px 16px;
    page-break-inside: avoid;
  }}
  .tech-blocked-title {{
    font-weight: 700; font-size: 11.5pt; color: #6b5a1f; margin-bottom: 6px;
  }}
  .tech-blocked-text {{
    font-size: 10.5pt; line-height: 1.5; color: #3d3a2f;
  }}
  .tech-grid {{
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
    page-break-inside: avoid;
  }}
  .tech-group {{
    background: #fafbf6; border: 1px solid #eef0e8;
    border-radius: 10px; padding: 12px 14px;
  }}
  .tech-group-title {{
    font-size: 9pt; font-weight: 800; text-transform: uppercase;
    letter-spacing: 0.07em; color: #1a1a1a;
    padding-bottom: 6px; margin-bottom: 6px;
    border-bottom: 1px solid #eef0e8;
  }}
  .tech-row {{
    display: flex; align-items: center; gap: 8px;
    padding: 4px 0; font-size: 10pt;
  }}
  .tech-row.ok {{ color: #1a1a1a; }}
  .tech-row.no {{ color: #1a1a1a; }}
  .tech-icon {{
    width: 16px; height: 16px;
    border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 9pt;
    flex-shrink: 0;
  }}
  .tech-row.ok .tech-icon {{ background: rgba(136,213,0,0.2); color: #4d7a00; }}
  .tech-row.no .tech-icon {{ background: rgba(239,68,68,0.15); color: #b91c1c; }}

  /* Cierre */
  .cierre-card {{
    background: #0a0a0a; color: #fff;
    border-radius: 14px; padding: 22px 26px;
    margin-top: 22px; page-break-inside: avoid;
  }}
  .cierre-card .hl-num {{ color: #88d500; }}
  .cierre-text {{ font-size: 11pt; line-height: 1.6; opacity: 0.95; }}
  .cierre-cta {{
    display: inline-block;
    background: #88d500; color: #0a0a0a;
    padding: 10px 20px; border-radius: 999px;
    font-weight: 700; font-size: 10.5pt; text-decoration: none;
    margin-top: 14px;
  }}

  /* Comparación con competidor */
  .comp-card {{
    border: 1px solid #eef0e8; border-radius: 12px;
    overflow: hidden; page-break-inside: avoid;
    margin-bottom: 18px; background: #fff;
  }}
  .comp-verdict {{
    padding: 14px 16px;
    background: #fafbf6;
    font-size: 10.5pt; line-height: 1.5;
    border-bottom: 1px solid #eef0e8;
  }}
  .comp-verdict strong {{ color: #4d7a00; }}
  .comp-scores {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 0;
    border-bottom: 1px solid #eef0e8;
  }}
  .comp-score {{
    padding: 14px; text-align: center;
  }}
  .comp-score.user-block {{
    background: #f4f8e8;
    border-right: 1px solid #eef0e8;
  }}
  .comp-score-label {{
    font-size: 8.5pt; text-transform: uppercase;
    letter-spacing: 0.07em; color: #5b6b4a;
    font-weight: 700; margin-bottom: 4px;
  }}
  .comp-score-value {{
    font-size: 24pt; font-weight: 900; line-height: 1;
  }}
  .comp-score-url {{
    margin-top: 4px; font-size: 8.5pt; color: #5b6b4a;
  }}
  .comp-list {{ padding: 4px 0; }}
  .comp-row {{
    display: grid;
    grid-template-columns: 1fr auto auto auto;
    gap: 10px; align-items: center;
    padding: 8px 14px;
    border-bottom: 1px dashed #eef0e8;
    font-size: 10pt;
  }}
  .comp-row:last-child {{ border-bottom: none; }}
  .comp-row-name {{ color: #1a1a1a; font-weight: 500; }}
  .comp-row-num {{
    width: 30px; text-align: center;
    font-size: 10pt; font-weight: 800;
    padding: 3px 0; border-radius: 6px;
    color: #fff;
  }}
  .comp-row-badge {{
    font-size: 8.5pt; font-weight: 800;
    padding: 3px 8px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.04em;
    min-width: 60px; text-align: center;
  }}
  .comp-row-badge.win {{ background: #e7f4cb; color: #4d7a00; }}
  .comp-row-badge.lose {{ background: #ffe1e1; color: #c00; }}
  .comp-row-badge.tie {{ background: #f0f2e8; color: #5b6b4a; }}

  .footer {{
    margin-top: 18px; padding-top: 12px;
    border-top: 1px solid #e2e8d4;
    font-size: 8.5pt; color: #5b6b4a;
    display: flex; justify-content: space-between;
  }}
  .footer a {{ color: #4d7a00; text-decoration: none; }}
</style>
</head>
<body>

  <div class="header">
    <div class="logo">Result<span class="dot">●</span></div>
    <div class="header-meta">
      Auditoría web<br>
      {_html_escape(fecha)}
    </div>
  </div>

  <div class="url-line"><strong>Sitio analizado:</strong> {_html_escape(url_analizada)}</div>

  <div class="score-section">
    <div class="score-box">
      <div class="score-num">{score}</div>
      <div class="score-num-sub">/10</div>
      <div class="score-label">Score general</div>
    </div>
    <div class="resumen">{resumen}</div>
  </div>

  <div class="section-title">Análisis por dimensión</div>
  <div class="dims-grid">{dims_html}</div>

  {comp_html}

  <div class="section-title">Problemas principales</div>
  {problemas_html}

  {tech_html}

  <div class="cierre-card">
    <div class="cierre-text">{cierre}</div>
    <a href="https://wa.me/5491126519615" class="cierre-cta">Agendar charla por WhatsApp</a>
  </div>

  <div class="footer">
    <div>RESULTO · Agencia de marketing digital</div>
    <div><a href="https://resulto.com.ar">resulto.com.ar</a></div>
  </div>

</body>
</html>"""


def renderizar_pdf_sync(html: str) -> bytes:
    """Toma un HTML y lo convierte en PDF usando Playwright."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html, wait_until="domcontentloaded")
        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        browser.close()
        return pdf_bytes


@app.get("/api/auditoria/{share_id}/pdf")
async def descargar_pdf(share_id: str):
    """Genera y devuelve un PDF de la auditoría guardada."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "SELECT resultado_json, url, created_at FROM auditorias WHERE share_id = ?",
            (share_id,),
        )
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Auditoría no encontrada")

    data = json.loads(row[0])
    url_analizada = row[1]
    fecha_raw = row[2] or ""
    # Formato fecha legible (toma sólo la parte de la fecha, sin hora)
    fecha = fecha_raw.split(" ")[0] if fecha_raw else ""

    html = generar_html_pdf(data, url_analizada, fecha)
    try:
        pdf_bytes = await asyncio.to_thread(renderizar_pdf_sync, html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No pude generar el PDF: {e}")

    # Nombre amigable basado en el dominio
    dominio = normalizar_url(url_analizada).replace("/", "_") or "auditoria"
    filename = f"auditoria-resulto-{dominio}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─────────────────────────────────────────────────────────────
# Export de leads (admin)
# ─────────────────────────────────────────────────────────────
@app.get("/admin/leads.csv")
def export_leads(token: str = ""):
    """Exporta todos los leads como CSV. Acceso protegido por token."""
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            SELECT id, share_id, email, url, rubro, tiempo, publicidad, objetivo, score, created_at
            FROM auditorias
            ORDER BY created_at DESC
            """
        )
        rows = cursor.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "share_id", "email", "url", "rubro", "tiempo",
        "publicidad", "objetivo", "score", "created_at",
    ])
    writer.writerows(rows)

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


@app.get("/admin/leads")
def listar_leads(token: str = ""):
    """Versión web: listado de leads en JSON, también protegido por token."""
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            SELECT id, share_id, email, url, rubro, score, created_at
            FROM auditorias
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    return {"total": len(rows), "leads": rows}
