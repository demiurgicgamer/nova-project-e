import 'dotenv/config';

const required = (key) => {
    const value = process.env[key];
    if (!value) throw new Error(`Missing required environment variable: ${key}`);
    return value;
};

export const env = {
    nodeEnv:   process.env.NODE_ENV || 'development',
    port:      parseInt(process.env.PORT || '3000', 10),
    isDev:     (process.env.NODE_ENV || 'development') === 'development',

    db: {
        url: required('DATABASE_URL'),
    },

    redis: {
        url:      required('REDIS_URL'),
        password: required('REDIS_PASSWORD'),
    },

    auth: {
        jwtSecret:        required('JWT_SECRET'),
        jwtRefreshSecret: required('JWT_REFRESH_SECRET'),
        jwtExpiresIn:     '24h',
        refreshExpiresIn: '30d',
    },

    apis: {
        claude:      required('CLAUDE_API_KEY'),
        deepgram:    required('DEEPGRAM_API_KEY'),
        elevenlabs:  required('ELEVENLABS_API_KEY'),
        hume:        required('HUME_API_KEY'),
    },

    agora: {
        appId:          required('AGORA_APP_ID'),
        appCertificate: required('AGORA_APP_CERTIFICATE'),
    },

    firebase: {
        projectId:    required('FIREBASE_PROJECT_ID'),
        privateKey:   required('FIREBASE_PRIVATE_KEY').replace(/\\n/g, '\n'),
        clientEmail:  required('FIREBASE_CLIENT_EMAIL'),
    },
};
