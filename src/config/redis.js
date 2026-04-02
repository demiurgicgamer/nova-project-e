import { createClient } from 'redis';
import { env } from './env.js';

const client = createClient({
    url:      env.redis.url,
    password: env.redis.password,
    socket: {
        reconnectStrategy: (retries) => {
            if (retries > 10) {
                console.error('[Redis] Max reconnect attempts reached.');
                return new Error('Redis max retries exceeded');
            }
            return Math.min(retries * 100, 3000); // exponential back-off up to 3s
        },
    },
});

client.on('error',   (err) => console.error('[Redis] Error:', err.message));
client.on('connect', ()    => console.log('[Redis] Connected'));
client.on('reconnecting', () => console.warn('[Redis] Reconnecting...'));

export const connectRedis = () => client.connect();

export default client;
