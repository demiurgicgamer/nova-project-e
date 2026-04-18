import http from 'http';
import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { env } from './config/env.js';
import { checkConnection } from './config/database.js';
import redisClient, { connectRedis } from './config/redis.js';
import { apiLimiter } from './middleware/rateLimiter.js';
import { errorHandler } from './middleware/errorHandler.js';
import authRoutes          from './routes/auth.js';
import consentRoutes       from './routes/consent.js';
import childrenRoutes      from './routes/children.js';
import agoraRoutes         from './routes/agora.js';
import notificationsRoutes from './routes/notifications.js';
import { SessionWebSocketServer } from './services/SessionWebSocketServer.js';
import { ElevenLabsTTSService }   from './services/ElevenLabsTTSService.js';

const app = express();

// ── Request logger (dev only) ─────────────────────────────────────────────────
if (env.isDev) {
    app.use((req, res, next) => {
        const start = Date.now();
        res.on('finish', () => {
            const ms = Date.now() - start;
            const flag = res.statusCode >= 400 ? ' ⚠' : '';
            console.log(`[${new Date().toISOString()}] ${req.method} ${req.path} → ${res.statusCode} (${ms}ms)${flag}`);
        });
        next();
    });
}

// ── Security middleware ───────────────────────────────────────────────────────
app.use(helmet());
app.use(cors({
    origin: env.isDev ? '*' : [], // lock down in production
    methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
}));
app.use(express.json({ limit: '1mb' }));
app.use(express.urlencoded({ extended: true }));
app.use(apiLimiter);

// ── Health check ──────────────────────────────────────────────────────────────
app.get('/health', async (_req, res) => {
    try {
        const dbTime = await checkConnection();
        res.json({ status: 'ok', db: dbTime, env: env.nodeEnv });
    } catch (err) {
        res.status(503).json({ status: 'error', message: err.message });
    }
});

// ── Routes ────────────────────────────────────────────────────────────────────
app.use('/api/auth',          authRoutes);
app.use('/api/consent',       consentRoutes);
app.use('/api/children',      childrenRoutes);
app.use('/api/agora',         agoraRoutes);
app.use('/api/notifications', notificationsRoutes);

// ── Dev-only TTS debug endpoint ───────────────────────────────────────────────
// GET  /api/debug/tts?text=Hello&lang=en
//   → returns JSON with PCM size, duration, first bytes, and base64 audio
// This bypasses WebSocket + Unity entirely — use curl or browser to verify TTS works.
if (env.isDev) {
    app.get('/api/debug/tts', async (req, res) => {
        const text = req.query.text ?? 'Hi, I am Ms. Nova, your math tutor. Hold the button to talk to me!';
        const lang = req.query.lang ?? 'en';

        console.log(`[DEBUG /api/debug/tts] text="${text}", lang=${lang}`);

        try {
            const tts = new ElevenLabsTTSService(redisClient);
            const pcm = await tts.synthesizeNova(text, lang);

            // Count non-zero bytes as a silence check
            let nonZeroBytes = 0;
            for (let i = 0; i < pcm.length; i++) {
                if (pcm[i] !== 0) nonZeroBytes++;
            }
            const silentPercent = ((1 - nonZeroBytes / pcm.length) * 100).toFixed(1);
            const durationSec   = (pcm.length / 2 / 22050).toFixed(2);
            const firstBytes    = pcm.slice(0, 16).toString('hex');
            const audioBase64   = pcm.toString('base64');

            console.log(`[DEBUG TTS] PCM=${pcm.length}B, duration=${durationSec}s, silent=${silentPercent}%, firstBytes=${firstBytes}`);

            res.json({
                ok:             true,
                text,
                lang,
                pcmBytes:       pcm.length,
                durationSec:    parseFloat(durationSec),
                silentPercent:  parseFloat(silentPercent),
                firstBytes,
                audioBase64,   // paste into base64decode.org and save as .wav to listen
            });
        } catch (err) {
            console.error(`[DEBUG TTS] Error: ${err.message}`, err.stack);
            res.status(500).json({ ok: false, error: err.message });
        }
    });
    console.log('[Nova API] Dev mode: TTS debug endpoint at GET /api/debug/tts');
}

// ── 404 handler ───────────────────────────────────────────────────────────────
app.use((_req, res) => res.status(404).json({ error: 'Not found' }));

// ── Centralised error handler (must be last) ──────────────────────────────────
app.use(errorHandler);

// ── Boot ──────────────────────────────────────────────────────────────────────
const start = async () => {
    await connectRedis();

    // Create HTTP server manually so WebSocket server can share the same port
    const httpServer = http.createServer(app);

    // Attach WebSocket session server (ws://host/session/{sessionId})
    const wss = new SessionWebSocketServer();
    wss.attach(httpServer);

    httpServer.listen(env.port, () => {
        console.log(`[Nova API] Running on port ${env.port} (${env.nodeEnv})`);
        console.log(`[Nova WSS] WebSocket listening on ws://localhost:${env.port}/session/{sessionId}`);
    });
};

start().catch((err) => {
    console.error('[Nova API] Failed to start:', err);
    process.exit(1);
});

export default app;
