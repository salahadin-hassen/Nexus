# 🛰 NEXUS — Orbital Tracking Platform

Real-time satellite tracking with night-lights Earth, SGP4 orbital mechanics, and a cinematic UI.

---

## Project Structure

```
nexus-deploy/
├── backend/
│   ├── main.py            ← FastAPI app (Python)
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── Procfile           ← Railway / Heroku
│   ├── railway.toml       ← Railway config
│   ├── render.yaml        ← Render config
│   └── fly.toml           ← Fly.io config
│
├── frontend/
│   ├── index.html         ← Complete single-file app
│   ├── vercel.json        ← Vercel config
│   └── netlify.toml       ← Netlify config
│
└── README.md
```

---

## ⚡ Deploy in 10 Minutes — Entirely Free

### Step 1 — Deploy the Backend (Pick ONE)

#### Option A: Railway (Recommended — easiest, free tier)

1. Go to [railway.app](https://railway.app) → Sign up with GitHub (free)
2. Click **New Project → Deploy from GitHub repo**
3. Push your `backend/` folder to a GitHub repo first, OR:
   - Click **New Project → Empty Project**
   - Click **Add Service → GitHub Repo**
   - Set **Root Directory** to `backend`
4. Railway auto-detects Python via `Procfile`
5. After deploy, copy your URL: `https://nexus-api-xxxx.railway.app`

**Free tier:** 500 hours/month — enough for 24/7 if you stay under limits.

---

#### Option B: Render (Free, sleeps after 15min inactivity)

1. Go to [render.com](https://render.com) → Sign up free
2. Click **New → Web Service**
3. Connect your GitHub repo, set **Root Directory** to `backend`
4. Render reads `render.yaml` automatically
5. After deploy, copy your URL: `https://nexus-api.onrender.com`

**Note:** Free tier sleeps after 15 min. First request after sleep takes ~30s.
The frontend handles this gracefully (falls back to demo data while backend wakes).

---

#### Option C: Fly.io (Always-on free tier)

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# From the backend/ directory:
cd backend
fly auth signup   # or fly auth login
fly launch        # follows fly.toml, auto-detects Python
fly deploy
```

Free allowance: 3 shared-CPU VMs, 256MB RAM each — perfect for this API.

---

### Step 2 — Deploy the Frontend (Pick ONE)

#### Option A: Vercel (Recommended — instant, free forever)

**Method 1 — Drag & drop (no CLI needed):**
1. Go to [vercel.com](https://vercel.com) → Sign up free
2. Click **Add New → Project**
3. Drag the `frontend/` folder into the upload area
4. Click **Deploy** — done in ~10 seconds

**Method 2 — CLI:**
```bash
npm i -g vercel
cd frontend
vercel --prod
```

---

#### Option B: Netlify (Also free forever)

**Drag & drop:**
1. Go to [netlify.com](https://netlify.com) → Sign up free
2. Drag the `frontend/` folder onto the deploy area at app.netlify.com/drop
3. Done — you get a URL instantly

**CLI:**
```bash
npm i -g netlify-cli
cd frontend
netlify deploy --prod --dir .
```

---

#### Option C: GitHub Pages (Free, requires a repo)

1. Create a GitHub repo
2. Push `frontend/index.html` to the `main` branch
3. Go to **Settings → Pages → Source: main branch / root**
4. Your site is live at `https://yourusername.github.io/repo-name`

---

### Step 3 — Connect Frontend to Backend

After deploying both, tell the frontend where your backend lives.

**Method 1 — URL query param (no code change needed):**
```
https://your-frontend.vercel.app?api=https://your-backend.railway.app
```

**Method 2 — Edit `index.html` (one line):**
Find this line in `index.html`:
```javascript
"http://localhost:8000"           // ← replace with your backend URL
```
Change it to:
```javascript
"https://your-backend.railway.app"
```
Then redeploy the frontend.

---

## 🏃 Run Locally

### Backend
```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start
uvicorn main:app --reload --port 8000
```
API available at http://localhost:8000
Docs at http://localhost:8000/docs

### Frontend
```bash
cd frontend

# Any static server works:
npx serve .                     # Node
python -m http.server 3000      # Python
# Or just open index.html directly in your browser
```

Open http://localhost:3000 (or use the ?api= param if backend is remote)

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check, used by platform monitors |
| GET | `/positions` | All satellite positions, propagated to now |
| GET | `/positions?group=starlink` | Filter by constellation |
| GET | `/positions?limit=500` | Cap result count |
| GET | `/meta` | Total counts, last refresh time |

### Example response `/positions`
```json
[
  {
    "id": 25544,
    "name": "ISS (ZARYA)",
    "group": "stations",
    "lat": 48.32,
    "lng": -122.87,
    "alt": 421.4,
    "vel": 7.668
  }
]
```

---

## Environment Variables (Backend)

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | HTTP port (set automatically by platforms) |
| `TLE_REFRESH_HOURS` | `6` | Hours between CelesTrak TLE refreshes |
| `MAX_SATS_PER_GROUP` | `500` | Cap per constellation |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |

---

## How It Works

```
CelesTrak (TLE data)
       ↓ every 6h
   FastAPI backend
   SGP4 propagation (sgp4 library)
   ECI → geodetic coords (Bowring method)
       ↓ every 5s
   globe.gl frontend
   Three.js rendering
   NASA night lights texture
   Custom satellite sprites (additive blending)
```

### Night Lights Rendering
The Earth uses NASA's city lights texture (`earth-night.jpg`) as both the
surface and emissive map. The Three.js directional light (sun) illuminates
the day side normally. On the night side, city lights glow through the
emissive channel. The brightness slider adjusts `emissiveIntensity`.

### Satellite Rendering
Each satellite is a Three.js `Group` containing:
- A dark contrast shadow sprite (normal blending)
- A colored halo sprite (additive blending)
- A bright core sprite (additive blending)
- A white pin sprite (always visible, depth test off)

This layered approach makes satellites visible against both the bright day
side and dark night side of Earth.

---

## Constellation Groups

| Key | Label | Color | Source |
|-----|-------|-------|--------|
| `starlink` | Starlink | Blue | CelesTrak starlink |
| `oneweb` | OneWeb | Orange | CelesTrak oneweb |
| `gps` | GPS | Green | CelesTrak gps-ops |
| `weather` | Weather | Cyan | CelesTrak weather |
| `stations` | Stations | Gold | CelesTrak stations |
| `iridium` | Iridium NEXT | — | CelesTrak iridium-NEXT |

---

## Free Tier Limits Summary

| Platform | Service | Limit | Sleep? |
|----------|---------|-------|--------|
| Railway | Backend | 500h/month | No |
| Render | Backend | 750h/month | Yes (15min) |
| Fly.io | Backend | 3 VMs free | No |
| Vercel | Frontend | 100GB bandwidth | No |
| Netlify | Frontend | 100GB bandwidth | No |
| GitHub Pages | Frontend | Unlimited | No |
