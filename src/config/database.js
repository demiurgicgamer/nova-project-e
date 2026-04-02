import pg from 'pg';
import { env } from './env.js';

const { Pool } = pg;

// ── Connection pool ───────────────────────────────────────────────────────────
const pool = new Pool({
    connectionString: env.db.url,
    ssl: env.isDev ? false : { rejectUnauthorized: true },
    max:              20,    // max connections in pool
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 5_000,
});

// Log unexpected pool-level errors (prevents unhandled rejections)
pool.on('error', (err) => {
    console.error('[DB] Unexpected pool error:', err.message);
});

// ── Public helpers ────────────────────────────────────────────────────────────

/** Run a single query. */
export const query = (text, params) => pool.query(text, params);

/**
 * Run multiple queries in a transaction.
 * @param {(client: pg.PoolClient) => Promise<T>} fn
 */
export const withTransaction = async (fn) => {
    const client = await pool.connect();
    try {
        await client.query('BEGIN');
        const result = await fn(client);
        await client.query('COMMIT');
        return result;
    } catch (err) {
        await client.query('ROLLBACK');
        throw err;
    } finally {
        client.release();
    }
};

/** Verify the database is reachable (used by /health endpoint). */
export const checkConnection = async () => {
    const result = await pool.query('SELECT NOW() AS now');
    return result.rows[0].now;
};

export default pool;
