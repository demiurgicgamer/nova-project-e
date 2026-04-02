/**
 * Curriculum seed script.
 * Loads curriculum.json and inserts topics + problems into PostgreSQL.
 * Safe to re-run — uses ON CONFLICT DO NOTHING for idempotency.
 *
 * Usage:
 *   node database/seed.js              — seed all curriculum data
 *   node database/seed.js --clear      — clear and re-seed (dev only)
 */

import fs   from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import pg      from 'pg';
import { v4 as uuidv4 } from 'uuid';
import dotenv  from 'dotenv';

dotenv.config();

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const pool = new pg.Pool({
    host:     process.env.POSTGRES_HOST     || 'localhost',
    port:     parseInt(process.env.POSTGRES_PORT || '5432'),
    database: process.env.POSTGRES_DB       || 'nova_db',
    user:     process.env.POSTGRES_USER     || 'nova_user',
    password: process.env.POSTGRES_PASSWORD || 'nova_password',
    ssl:      process.env.DB_SSL === 'true' ? { rejectUnauthorized: false } : false,
});

async function seed(clear = false) {
    const client = await pool.connect();

    try {
        const dataPath = path.join(__dirname, 'seeds', 'curriculum.json');
        const data     = JSON.parse(fs.readFileSync(dataPath, 'utf8'));

        if (clear) {
            console.log('🗑️  Clearing existing curriculum data...');
            await client.query('DELETE FROM curriculum_problems');
            await client.query('DELETE FROM curriculum_topics');
            console.log('   Done.\n');
        }

        let topicsInserted   = 0;
        let problemsInserted = 0;

        console.log(`📚 Seeding ${data.topics.length} topics...\n`);

        for (const topic of data.topics) {
            // Upsert topic — skip if already exists (grade + topic_key unique)
            const topicResult = await client.query(
                `INSERT INTO curriculum_topics (id, grade, subject, topic_key, display_name, order_index)
                 VALUES ($1, $2, $3, $4, $5, $6)
                 ON CONFLICT (grade, topic_key) DO UPDATE
                     SET display_name = EXCLUDED.display_name,
                         order_index  = EXCLUDED.order_index
                 RETURNING id`,
                [uuidv4(), topic.grade, topic.subject, topic.topic_key, topic.display_name, topic.order_index]
            );

            const topicId = topicResult.rows[0].id;
            topicsInserted++;

            process.stdout.write(`  ▶ Grade ${topic.grade} — ${topic.display_name} (${topic.problems.length} problems) ... `);

            for (const problem of topic.problems) {
                await client.query(
                    `INSERT INTO curriculum_problems
                        (id, topic_id, language_code, difficulty, problem_text, solution_steps, cultural_context)
                     VALUES ($1, $2, $3, $4, $5, $6, $7)
                     ON CONFLICT DO NOTHING`,
                    [
                        uuidv4(),
                        topicId,
                        problem.language_code,
                        problem.difficulty,
                        problem.problem_text,
                        JSON.stringify(problem.solution_steps),
                        problem.cultural_context || null,
                    ]
                );
                problemsInserted++;
            }

            console.log('✅');
        }

        console.log(`\n✅ Seeded ${topicsInserted} topics and ${problemsInserted} problems.`);

        // Summary
        const topicCount   = await client.query('SELECT COUNT(*) FROM curriculum_topics');
        const problemCount = await client.query('SELECT COUNT(*) FROM curriculum_problems');
        console.log(`\n📊 Database totals: ${topicCount.rows[0].count} topics, ${problemCount.rows[0].count} problems`);

    } finally {
        client.release();
        await pool.end();
    }
}

const args  = process.argv.slice(2);
const clear = args.includes('--clear');

seed(clear).catch(err => {
    console.error('❌ Seed failed:', err.message);
    process.exit(1);
});
