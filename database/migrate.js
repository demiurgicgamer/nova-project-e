/**
 * Simple sequential migration runner.
 * Runs all SQL files in /database/migrations/ in filename order.
 * Tracks applied migrations in a `schema_migrations` table so each
 * file is only ever executed once.
 *
 * Usage:
 *   node database/migrate.js            — run all pending migrations
 *   node database/migrate.js --status   — list applied / pending migrations
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import pg from 'pg';
import dotenv from 'dotenv';

dotenv.config();

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MIGRATIONS_DIR = path.join(__dirname, 'migrations');

const pool = new pg.Pool({
  host:     process.env.POSTGRES_HOST     || 'localhost',
  port:     parseInt(process.env.POSTGRES_PORT || '5432'),
  database: process.env.POSTGRES_DB       || 'nova_db',
  user:     process.env.POSTGRES_USER     || 'nova_user',
  password: process.env.POSTGRES_PASSWORD || 'nova_password',
  ssl:      process.env.DB_SSL === 'true' ? { rejectUnauthorized: false } : false,
});

async function ensureMigrationsTable(client) {
  await client.query(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      filename   TEXT PRIMARY KEY,
      applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
}

async function getAppliedMigrations(client) {
  const result = await client.query('SELECT filename FROM schema_migrations ORDER BY filename');
  return new Set(result.rows.map(r => r.filename));
}

function getMigrationFiles() {
  return fs
    .readdirSync(MIGRATIONS_DIR)
    .filter(f => f.endsWith('.sql'))
    .sort(); // lexicographic = numeric order (001, 002, ...)
}

async function runMigrations() {
  const client = await pool.connect();

  try {
    await ensureMigrationsTable(client);
    const applied = await getAppliedMigrations(client);
    const files   = getMigrationFiles();

    const pending = files.filter(f => !applied.has(f));

    if (pending.length === 0) {
      console.log('✅ All migrations already applied — nothing to do.');
      return;
    }

    console.log(`🚀 Running ${pending.length} pending migration(s)...\n`);

    for (const filename of pending) {
      const filePath = path.join(MIGRATIONS_DIR, filename);
      const sql      = fs.readFileSync(filePath, 'utf8');

      process.stdout.write(`  ▶ ${filename} ... `);

      try {
        await client.query('BEGIN');
        await client.query(sql);
        await client.query(
          'INSERT INTO schema_migrations (filename) VALUES ($1)',
          [filename]
        );
        await client.query('COMMIT');
        console.log('✅ done');
      } catch (err) {
        await client.query('ROLLBACK');
        console.error(`\n❌ FAILED: ${err.message}`);
        console.error(`   File: ${filePath}`);
        process.exit(1);
      }
    }

    console.log('\n✅ All migrations applied successfully.');
  } finally {
    client.release();
    await pool.end();
  }
}

async function showStatus() {
  const client = await pool.connect();

  try {
    await ensureMigrationsTable(client);
    const applied = await getAppliedMigrations(client);
    const files   = getMigrationFiles();

    console.log('\nMigration Status:\n');
    for (const f of files) {
      const status = applied.has(f) ? '✅ applied' : '⏳ pending';
      console.log(`  ${status}  ${f}`);
    }

    const unknown = [...applied].filter(f => !files.includes(f));
    for (const f of unknown) {
      console.log(`  ⚠️  in DB but no file  ${f}`);
    }
    console.log('');
  } finally {
    client.release();
    await pool.end();
  }
}

// ── Entry point ──────────────────────────────────────────────────────────────
const args = process.argv.slice(2);

if (args.includes('--status')) {
  showStatus().catch(err => { console.error(err); process.exit(1); });
} else {
  runMigrations().catch(err => { console.error(err); process.exit(1); });
}
