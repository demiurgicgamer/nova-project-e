import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { env } from './config/env.js';
import { checkConnection } from './config/database.js';
import { connectRedis } from './config/redis.js';
import { apiLimiter } from './middleware/rateLimiter.js';
import { errorHandler } from './middleware/errorHandler.js';
import authRoutes from './routes/auth.js';

const app = express();

// ── Security middleware ───────────────────────────────────────────────────────
app.use(helmet());
app.use(cors({
    origin: env.isDev ? '*' : [], // lock down in production
    methods: ['GET', 'POST', 'PUT', 'DELETE'],
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
app.use('/api/auth',     authRoutes);
// app.use('/api/children', childRoutes);   // Day 20

// ── 404 handler ───────────────────────────────────────────────────────────────
app.use((_req, res) => res.status(404).json({ error: 'Not found' }));

// ── Centralised error handler (must be last) ──────────────────────────────────
app.use(errorHandler);

// ── Boot ──────────────────────────────────────────────────────────────────────
const start = async () => {
    await connectRedis();
    app.listen(env.port, () => {
        console.log(`[Nova API] Running on port ${env.port} (${env.nodeEnv})`);
    });
};

start().catch((err) => {
    console.error('[Nova API] Failed to start:', err);
    process.exit(1);
});

export default app;
