import 'dotenv/config';

const required = (key) => {
    const value = process.env[key];
    if (!value) throw new Error(`Missing required environment variable: ${key}`);
    return value;
};

// Optional in Month 1 — returns value or 'placeholder' without crashing
const optional = (key, fallback = 'placeholder') => process.env[key] || fallback;

// Build a postgres connection URL from individual POSTGRES_* vars
const buildPostgresUrl = () => {
    const host     = process.env.POSTGRES_HOST     || 'localhost';
    const port     = process.env.POSTGRES_PORT     || '5432';
    const db       = process.env.POSTGRES_DB       || 'nova_db';
    const user     = process.env.POSTGRES_USER     || 'nova_user';
    const password = process.env.POSTGRES_PASSWORD || 'nova_password';
    return `postgresql://${user}:${password}@${host}:${port}/${db}`;
};

const buildRedisUrl = () => {
    const host     = process.env.REDIS_HOST     || 'localhost';
    const port     = process.env.REDIS_PORT     || '6379';
    const password = process.env.REDIS_PASSWORD || '';
    return password
        ? `redis://:${password}@${host}:${port}`
        : `redis://${host}:${port}`;
};

export const env = {
    nodeEnv: process.env.NODE_ENV || 'development',
    port:    parseInt(process.env.API_PORT || process.env.PORT || '3000', 10),
    isDev:   (process.env.NODE_ENV || 'development') === 'development',

    db: {
        // Accept explicit DATABASE_URL or build from POSTGRES_* vars
        url: process.env.DATABASE_URL || buildPostgresUrl(),
    },

    redis: {
        url:      process.env.REDIS_URL || buildRedisUrl(),
        password: process.env.REDIS_PASSWORD || '',
    },

    auth: {
        jwtSecret:        required('JWT_SECRET'),
        jwtRefreshSecret: required('JWT_REFRESH_SECRET'),
        jwtExpiresIn:     '24h',
        refreshExpiresIn: '30d',
    },

    // Month 2+ APIs — placeholder values are fine during Month 1
    apis: {
        claude:     optional('CLAUDE_API_KEY'),
        deepgram:   optional('DEEPGRAM_API_KEY'),
        elevenlabs: optional('ELEVENLABS_API_KEY'),
        hume:       optional('HUME_API_KEY'),
    },

    agora: {
        appId:          optional('AGORA_APP_ID'),
        appCertificate: optional('AGORA_APP_CERTIFICATE'),
    },

    firebase: {
        projectId:   optional('FIREBASE_PROJECT_ID'),
        privateKey:  optional('FIREBASE_PRIVATE_KEY').replace(/\\n/g, '\n'),
        clientEmail: optional('FIREBASE_CLIENT_EMAIL'),
    },
};
