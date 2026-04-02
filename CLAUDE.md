# Nova Backend — AI Tutor API Server
**Part of:** AI Tutor MVP — Ms. Nova (Android)
**Repo:** Nova-Backend (separate from Unity project)
**Language:** Node.js (Express) + Python (LangGraph — Month 2)
**Start Date:** April 2, 2026

---

## What This Repo Does

This is the backend for the Ms. Nova AI tutoring app. It handles:
- Parent authentication (Firebase + JWT)
- Child profile management
- Tutoring session lifecycle
- Voice pipeline orchestration (Deepgram STT → Claude agent → ElevenLabs TTS)
- Real-time WebSocket events to Unity client
- Emotion data processing (Hume AI)
- Curriculum and progress tracking (PostgreSQL)
- Session state caching (Redis)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Node.js 20 (ESM modules) |
| Framework | Express.js |
| AI Agent | Python + LangGraph + Claude API (Month 2) |
| Database | PostgreSQL 15 (pg pool) |
| Cache | Redis 7 |
| Auth | Firebase Admin SDK + JWT (jsonwebtoken) |
| Voice STT | Deepgram SDK (Month 2) |
| Voice TTS | ElevenLabs SDK (Month 2) |
| Emotion AI | Hume AI (Month 3) |
| Real-time | ws (WebSocket), Agora (Month 2) |
| Containers | Docker + docker-compose |

---

## Project Structure

```
Nova-Backend/
├── src/
│   ├── app.js                   # Express entry point, middleware wiring
│   ├── config/
│   │   ├── env.js               # Validated env vars (throws on missing required)
│   │   ├── database.js          # pg connection pool, query(), withTransaction()
│   │   ├── redis.js             # Redis client with reconnect strategy
│   │   └── firebase.js          # Firebase Admin SDK (lazy init, placeholder-safe)
│   ├── middleware/
│   │   ├── auth.js              # JWT verify, signAccessToken, signRefreshToken
│   │   ├── firebaseAuth.js      # Firebase ID token verifier
│   │   ├── rateLimiter.js       # authLimiter (10/15min), apiLimiter (100/min)
│   │   └── errorHandler.js      # Centralised error handler + asyncHandler wrapper
│   ├── routes/
│   │   └── auth.js              # POST /api/auth/register, /login, /refresh
│   ├── controllers/
│   │   └── authController.js    # register, login, refresh handlers
│   └── services/                # Voice, agent, session services (Month 2+)
├── database/
│   └── migrations/
│       └── 001_create_parent_profiles.sql
├── docker-compose.yml           # api + postgres + redis with health checks
├── Dockerfile                   # Node 20 Alpine, nodemon in dev
├── .env.example                 # Template — copy to .env, never commit .env
└── package.json                 # "type": "module" (ESM)
```

---

## Running Locally

```bash
# 1. Copy env template and fill in values
cp .env.example .env

# 2. Start all containers
docker compose up -d

# 3. Verify everything is healthy
curl http://localhost:3000/health
# → {"status":"ok","db":"...","env":"development"}

# 4. View logs
docker logs nova_api -f
```

### Ports
| Service | Port |
|---|---|
| Node.js API | `3000` |
| PostgreSQL | `5432` |
| Redis | `6379` |

---

## API Endpoints

### Auth (`/api/auth`)
| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/auth/register` | Firebase token | Create parent account |
| `POST` | `/api/auth/login` | Firebase token | Login, get JWT pair |
| `POST` | `/api/auth/refresh` | Refresh token in body | Get new access token |
| `GET` | `/health` | None | DB + server health check |

> **Note:** Register/Login require a real Firebase project configured in `.env`.
> With placeholder credentials the server starts fine but returns `503` on auth routes.

### Coming in Month 2
- `POST /api/sessions/start` — create session, spawn AI agent
- `POST /api/sessions/:id/end` — save session results
- `GET  /api/children/:id/progress` — topic mastery data
- `WS   /ws` — WebSocket event stream to Unity

---

## Database Migrations

Migrations live in `database/migrations/` and are named `NNN_description.sql`.
Run them manually against the Docker container:

```bash
docker exec -i nova_postgres psql -U nova_user -d nova_db < database/migrations/001_create_parent_profiles.sql
```

### Current Schema
| Table | Status | Migration |
|---|---|---|
| `parent_profiles` | ✅ Live | `001_create_parent_profiles.sql` |
| `child_profiles` | Month 1 Week 4 | `002_...` |
| `sessions` | Month 1 Week 4 | `002_...` |
| `curriculum_topics` | Month 1 Week 4 | `002_...` |
| `curriculum_problems` | Month 1 Week 4 | `002_...` |
| `child_topic_progress` | Month 1 Week 4 | `002_...` |

---

## Environment Variables

Copy `.env.example` to `.env`. Required keys:

| Key | When Needed | Notes |
|---|---|---|
| `POSTGRES_*` | Now | Local dev values in `.env` |
| `REDIS_PASSWORD` | Now | Local dev value in `.env` |
| `JWT_SECRET` / `JWT_REFRESH_SECRET` | Now | Min 32 chars |
| `FIREBASE_*` | Month 2 | From Firebase Console → Service Account |
| `CLAUDE_API_KEY` | Month 2 | From console.anthropic.com |
| `DEEPGRAM_API_KEY` | Month 2 | From console.deepgram.com |
| `ELEVENLABS_API_KEY` | Month 2 | From elevenlabs.io |
| `HUME_API_KEY` | Month 3 | From hume.ai |
| `AGORA_*` | Month 2 | From console.agora.io |

---

## Working With Claude Code

### Code Style
- ESM imports (`import/export`) throughout — no `require()`
- `asyncHandler(fn)` wrapper on all async route handlers — no try/catch in controllers
- `withTransaction(client => ...)` for multi-query DB operations
- Always use parameterised queries (`$1, $2`) — never string interpolation in SQL

### Adding a New Route
1. Create `src/controllers/newController.js`
2. Create `src/routes/new.js` — apply `asyncHandler` to all handlers
3. Register in `src/app.js`: `app.use('/api/new', newRoutes)`
4. Add migration if new DB table needed

### Security Rules (Non-Negotiable)
- All child data endpoints must use `requireAuth` middleware
- Never log JWT tokens, Firebase tokens, or passwords
- All SQL uses parameterised queries
- Rate limiter on all public-facing routes

---

## Month-by-Month Backend Roadmap

| Month | Key Backend Work |
|---|---|
| 1 (Apr) | ✅ Docker setup, auth routes, parent_profiles table |
| 1 Week 4 | Full PostgreSQL schema (all tables) |
| 2 (Apr–May) | Deepgram STT, ElevenLabs TTS, WebSocket server, AI agent (LangGraph) |
| 3 (May–Jun) | Hume AI emotion pipeline, adaptive difficulty engine, profile learning |
| 4 (Jun–Jul) | Full curriculum seed, security audit, subscription (Google Play Billing) |
| 5–6 (Jul–Sep) | Load testing, auto-scaling, production infra (AWS/GCP) |

---

*Backend for Ms. Nova AI Tutor — built with Claude Code.*
