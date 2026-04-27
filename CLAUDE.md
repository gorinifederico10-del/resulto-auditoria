# RESULTO Auditoría Tool — Contexto del proyecto

Este archivo te pone al tanto de qué es este proyecto, qué está hecho y qué falta.

## Qué es

Herramienta de lead-gen para **RESULTO**, agencia de marketing del usuario (Federico Gorini, alias Fede). Toma una URL + datos del negocio y devuelve un diagnóstico honesto del estado de la web hablado en lenguaje de dueño (no de técnico). Es parte de la tesis "cómo armar una agencia de marketing desde cero".

URL final esperada: `resulto.com.ar/auditoria` — pero hoy todavía corre standalone.

## Stack actual

- **Backend:** Python 3 + FastAPI + uvicorn (puerto 8001).
- **Análisis:** Playwright (navegador headless con screenshots), Google PageSpeed Insights API (sin key), Gemini 2.5 Flash multimodal (texto + imágenes).
- **Frontend:** un `index.html` standalone servido desde `/` por FastAPI. Tailwind/CSS hecho a mano, JS vanilla.
- **DB:** SQLite (`auditorias.db`) — guarda leads (email + URL + resultado) y permite compartir por link.
- **IA:** `gemini-2.5-flash` con `temperature=0`, `top_k=1` para resultados consistentes.

## Estructura

```
brieftool/
├── main.py                  ← FastAPI: scraping, Playwright, Gemini, endpoints
├── index.html               ← UI completa (form + loader + resultado)
├── auditorias.db            ← SQLite (se crea solo)
├── .env                     ← GEMINI_API_KEY, ALLOWED_ORIGINS, ADMIN_TOKEN, CACHE_HOURS
└── requirements.txt
```

## Lo que ya está implementado

1. **Form + análisis streaming.** Email + URL + rubro + tiempo + publicidad + objetivo. NDJSON streaming con barra de progreso real (no truchada).
2. **8 dimensiones puntuadas 0-10:** diseño visual, experiencia moderna, claridad de propuesta, credibilidad, llamado a la acción, contenido relevante, encontrabilidad (SEO), velocidad técnica.
3. **Score promedio + top 4 problemas** estructurados en framework SPIN: qué pasa / qué te cuesta / por qué no lo resolvés solo.
4. **Setup técnico oculto** (sección abajo del resultado): detecta Pixel de Meta, Google Analytics, GTM, TikTok Pixel, Hotjar, Schema markup, Open Graph, HTTPS, title, meta description, canonical, lang, favicon. Resumen con severidad real por pesos.
5. **Caché por URL de 24h** (`CACHE_HOURS` en .env): misma URL = mismo resultado durante el período. Igual guarda el lead nuevo.
6. **Botón Compartir** que copia un link tipo `?share=abc123` que renderiza el resultado guardado.
7. **Endpoints admin:** `/admin/leads.csv?token=X` para descargar leads, `/admin/leads?token=X` para JSON.
8. **Logo clickeable** vuelve al inicio.
9. **Animaciones del hero:** canvas con red de partículas, palabras rotatorias en stack (sin layout shift).

## Lo que falta (en orden)

1. **PDF descargable** de la auditoría — pendiente. La idea es usar Playwright (ya instalado) para generar un PDF server-side desde un endpoint tipo `/auditoria/{share_id}/pdf`.
2. **Análisis de competencia** — pendiente. Campo opcional de URL competidor, correr la auditoría dos veces, mostrar comparación dimensión por dimensión. **CRÍTICO:** debe ser honesto — no asumir que el competidor siempre está mejor. Mostrar incluso donde el usuario gana.
3. **Integrar dentro de la web Next.js de RESULTO** (`resulto-web/`). Hay `audit-tool.tsx` y `auditoria-page.tsx` ya armados pero en pausa. La idea final es Vercel (web Next.js) + Railway (backend Python) con CORS configurado.

## Cómo correr local

```bash
cd C:\Users\gorin\proyectos\brieftool
python -m uvicorn main:app --reload --port 8001
```

Abrí `http://localhost:8001`. Si algo cambia se recarga solo.

## Cómo hablar con Fede

- Español rioplatense (vos, no tú), informal pero **profesional, no chabacano**.
- Al grano, sin disclaimers ni dorada de píldora.
- Directo y honesto: si algo está flojo, decirlo.
- Federico no es desarrollador. Es analista de marketing. Si proponés algo técnico, explicalo en pasos concretos, sin asumir conocimiento.
- **No usar:** "tirar guita", "estás al horno", "una papa", "volás a ciegas". Sí usar: "lenguaje de dueño", explicaciones tipo "como un consultor le explica a un cliente".

## Decisiones tomadas

- **In-house first:** primero terminamos features acá (standalone) y después integramos con Next.js.
- **Gemini sobre OpenAI:** porque es multimodal, gratis hasta cierto rate y suficiente.
- **SQLite sobre Postgres:** simple, sin deps extra, suficiente para esto.
- **Caché por URL:** misma URL = mismo resultado durante 24h.
- **Temperatura 0:** consistencia ante todo.

## Reglas de scoring del prompt

- Web promedio de pyme = 3-4/10.
- Solo top 20% merece 6+.
- Solo top 5% merece 8+.
- 9-10 reservado para nivel internacional premium (Apple, Stripe, Notion).
- NO quedarse en el medio. Usar todo el rango.
