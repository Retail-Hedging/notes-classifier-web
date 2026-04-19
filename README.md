# notes-classifier-web

Mobile-first Tinder-style UI for triaging Signal Found conversations stuck in
`UNKNOWN` state on active campaigns. One card per conversation; tap a state to
call `change_crm_state` and auto-advance to the next.

## Architecture

- **Backend:** FastAPI (`backend/app.py`), reuses notes-bot's `mcp_client`
- **Frontend:** React + Vite + TypeScript + Tailwind
- **Deploy:** systemd unit `notes-classifier-web.service`, binds `0.0.0.0:8000`
- **Auth:** shared bearer token from `backend/.env` (`APP_TOKEN`); frontend
  reads `?token=…` on first load and stores in `localStorage`

## Endpoints

| | |
|---|---|
| `GET /api/health` | liveness |
| `GET /api/unknown` | list UNKNOWN convos on active campaigns |
| `GET /api/conversation` | messages + product strategy for one convo |
| `POST /api/classify` | body `{product_slug, customer_name, new_state}` |
| `GET /` + `/assets/*` | static React build |

All `/api/*` except `/api/health` require `Authorization: Bearer $APP_TOKEN`.

## Local dev

```bash
# backend
cd backend
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env && edit .env   # set APP_TOKEN
venv/bin/uvicorn app:app --reload --port 8000

# frontend
cd frontend
npm install
npm run dev    # Vite dev server proxies /api to :8000
npm run build  # production build → frontend/dist, served by FastAPI
```

## Deploy

```bash
cd frontend && npm run build
systemctl enable --now notes-classifier-web
```

Open `http://$VPS_IP:8000/?token=$APP_TOKEN` on a phone.
