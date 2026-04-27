# Deploy a Railway — paso a paso

Tiempo estimado: **5-10 minutos**. Lo único que necesitás hacer vos es lo que requiere autenticación con tu cuenta (GitHub, Railway). Lo demás está todo armado.

---

## Antes de arrancar

Cuenta de Railway (gratis, USD 5/mes de crédito): https://railway.com
Cuenta de GitHub (gratis): https://github.com
La carpeta del proyecto ya tiene todo lo necesario: `Dockerfile`, `railway.json`, `requirements.txt`, `.gitignore`, `.env.example`.

---

## 1. Subí el código a un repo de GitHub

Abrí PowerShell en la carpeta del proyecto:

```powershell
cd C:\Users\gorin\proyectos\brieftool
git init
git add .
git commit -m "Initial commit — RESULTO Auditoría Tool"
```

Después en GitHub: **New repository** → nombre `resulto-auditoria` (o lo que prefieras) → **Private** está OK → **Create**.

Te muestra dos comandos para conectar el repo local. Pegalos:

```powershell
git remote add origin https://github.com/<tu-usuario>/resulto-auditoria.git
git branch -M main
git push -u origin main
```

> El `.gitignore` ya está configurado para NO subir `.env` ni `auditorias.db`. Tus secretos quedan en tu máquina.

---

## 2. Crear el proyecto en Railway

1. Andá a https://railway.com/new y logueate con GitHub.
2. **Deploy from GitHub repo** → seleccioná `resulto-auditoria`.
3. Railway detecta el `Dockerfile` automáticamente y arranca el build.

---

## 3. Configurar las variables de entorno

En Railway, en el service que se creó:

**Variables** (tab) → **+ New Variable** y agregá una por una:

| Nombre              | Valor                                                                |
|---------------------|----------------------------------------------------------------------|
| `GEMINI_API_KEY`    | (la misma que tenés en tu `.env` local)                              |
| `ALLOWED_ORIGINS`   | `https://resulto.com.ar,https://www.resulto.com.ar`                  |
| `ADMIN_TOKEN`       | inventá una string larga al azar                                     |
| `CACHE_HOURS`       | `24`                                                                 |
| `DB_PATH`           | `/data/auditorias.db`                                                |

> Tip: en el archivo `.env.example` está esto mismo de referencia.

Después de agregar las variables, Railway redeploya solo.

---

## 4. Persistencia de la DB (importante)

Para que los leads no se borren en cada deploy, agregale un **volumen**:

En el service → **Settings** → **Volumes** → **+ New Volume**.
Mount path: `/data`
Tamaño: 1 GB sobra.

(Por eso la variable `DB_PATH` apunta a `/data/auditorias.db`.)

---

## 5. Generar la URL pública

En el service → **Settings** → **Networking** → **Generate Domain**.

Te tira un link tipo `https://resulto-auditoria-production.up.railway.app`.

Si querés un nombre más lindo, en **Settings** podés editar el subdominio:
`resulto-auditoria.up.railway.app` (o lo que esté libre).

**Ese es el link para compartir.**

---

## 6. (Opcional) Conectarlo a tu dominio `resulto.com.ar`

Si querés que el link sea `auditoria.resulto.com.ar` o `resulto.com.ar/auditoria`:

Opción A — subdominio (más fácil):
1. En Railway: **Settings** → **Networking** → **+ Custom Domain** → escribí `auditoria.resulto.com.ar`.
2. Railway te muestra un valor CNAME.
3. En el panel DNS de tu dominio (donde lo compraste): creá un registro CNAME `auditoria` apuntando a ese valor.
4. Listo, en 5-30 minutos propaga.

Opción B — `resulto.com.ar/auditoria` (path):
Esto requiere que tu web Next.js (en Vercel) haga un proxy/rewrite hacia el backend Railway. Es la integración final que tenías en el roadmap. Para eso, decime y te dejo armado el `next.config.js` con el rewrite.

---

## 7. Verificar que anduvo

Abrí el link público y probá una auditoría. Si funciona, listo.

Si tira error, en Railway → **Deployments** → última deploy → **View Logs** y mandame el error.

---

## Costo

Railway tier gratis te da USD 5/mes de crédito. Esta app, con uso bajo (10-50 auditorías/mes), entra cómoda. Si crece, con USD 5-10/mes pagos te alcanza para varios miles.

---

## Cambios futuros

Cada vez que hagas `git push origin main`, Railway redeploya solo. No hay que tocar nada más.
