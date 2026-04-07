import { query } from '../config/database.js';

/**
 * Progress controller — manages child_topic_progress records.
 *
 * All endpoints verify that the authenticated parent owns the requested child
 * before reading or writing any progress data.
 */

// ── Ownership guard ───────────────────────────────────────────────────────────

/**
 * Returns the child row if it belongs to the requesting parent, else null.
 */
const getOwnedChild = async (parentId, childId) => {
    const result = await query(
        'SELECT id FROM child_profiles WHERE id = $1 AND parent_id = $2',
        [childId, parentId]
    );
    return result.rows[0] ?? null;
};

// ── GET /api/children/:id/progress ───────────────────────────────────────────

/**
 * Returns all topic progress rows for a child, joined with curriculum_topics
 * so Unity receives topic_key + display_name instead of raw UUIDs.
 *
 * Response shape (Unity-friendly — JsonUtility requires a wrapper object):
 * {
 *   "progress": [
 *     {
 *       "topicKey":    "ratios",
 *       "displayName": "Ratios",
 *       "grade":       6,
 *       "subject":     "mathematics",
 *       "masteryLevel": 72,        // 0–100
 *       "status":      "progressing", // not_started | progressing | mastered
 *       "correctCount":  9,
 *       "attemptCount": 13,
 *       "lastAttempted": "2026-04-07T10:00:00Z" // null if never attempted
 *     }
 *   ]
 * }
 */
export const getProgress = async (req, res) => {
    const parentId = req.user.sub;
    const { id: childId } = req.params;

    const child = await getOwnedChild(parentId, childId);
    if (!child) return res.status(404).json({ error: 'Child not found.' });

    const result = await query(
        `SELECT
            ct.topic_key       AS "topicKey",
            ct.display_name    AS "displayName",
            ct.grade,
            ct.subject,
            ctp.mastery_level  AS "masteryLevel",
            ctp.status,
            ctp.correct_count  AS "correctCount",
            ctp.attempt_count  AS "attemptCount",
            ctp.last_attempted AS "lastAttempted"
         FROM curriculum_topics ct
         LEFT JOIN child_topic_progress ctp
                ON ctp.topic_id = ct.id AND ctp.child_id = $1
         WHERE ct.grade = (SELECT grade FROM child_profiles WHERE id = $1)
         ORDER BY ct.order_index ASC`,
        [childId]
    );

    // For topics with no progress row yet, fill in safe defaults
    const progress = result.rows.map(row => ({
        topicKey:     row.topicKey,
        displayName:  row.displayName,
        grade:        row.grade,
        subject:      row.subject,
        masteryLevel: row.masteryLevel ?? 0,
        status:       row.status       ?? 'not_started',
        correctCount: row.correctCount ?? 0,
        attemptCount: row.attemptCount ?? 0,
        lastAttempted: row.lastAttempted ?? null,
    }));

    return res.status(200).json({ progress });
};

// ── PUT /api/children/:id/progress/:topicKey ──────────────────────────────────

/**
 * Upserts progress for a single topic. Called by Unity after each session.
 *
 * Body: {
 *   "masteryLevel":  72,   // 0–100  (required)
 *   "correctCount":   9,   // cumulative
 *   "attemptCount":  13    // cumulative
 * }
 *
 * Derives status automatically:
 *   < 40  → not_started (or needs_review if ever attempted)
 *   40–74 → progressing
 *   ≥ 75  → mastered
 */
export const updateProgress = async (req, res) => {
    const parentId = req.user.sub;
    const { id: childId, topicKey } = req.params;
    const { masteryLevel, correctCount, attemptCount } = req.body;

    // ── Validate ──────────────────────────────────────────────────────────────
    if (typeof masteryLevel !== 'number' || masteryLevel < 0 || masteryLevel > 100) {
        return res.status(400).json({ error: 'masteryLevel must be a number between 0 and 100.' });
    }

    // ── Ownership check ───────────────────────────────────────────────────────
    const child = await getOwnedChild(parentId, childId);
    if (!child) return res.status(404).json({ error: 'Child not found.' });

    // ── Resolve topic UUID from topic_key + child's grade ────────────────────
    const topicResult = await query(
        `SELECT ct.id FROM curriculum_topics ct
         JOIN child_profiles cp ON cp.id = $1
         WHERE ct.topic_key = $2 AND ct.grade = cp.grade`,
        [childId, topicKey]
    );

    if (topicResult.rows.length === 0) {
        return res.status(404).json({ error: `Topic '${topicKey}' not found for this child's grade.` });
    }

    const topicId = topicResult.rows[0].id;

    // ── Derive status ─────────────────────────────────────────────────────────
    let status;
    if (masteryLevel >= 75)      status = 'mastered';
    else if (masteryLevel >= 40) status = 'progressing';
    else if ((attemptCount ?? 0) > 0) status = 'needs_review';
    else                         status = 'not_started';

    // ── Upsert ────────────────────────────────────────────────────────────────
    const result = await query(
        `INSERT INTO child_topic_progress
             (child_id, topic_id, mastery_level, status, correct_count, attempt_count, last_attempted)
         VALUES ($1, $2, $3, $4, $5, $6, NOW())
         ON CONFLICT (child_id, topic_id) DO UPDATE SET
             mastery_level  = EXCLUDED.mastery_level,
             status         = EXCLUDED.status,
             correct_count  = EXCLUDED.correct_count,
             attempt_count  = EXCLUDED.attempt_count,
             last_attempted = NOW()
         RETURNING
             mastery_level  AS "masteryLevel",
             status,
             correct_count  AS "correctCount",
             attempt_count  AS "attemptCount",
             last_attempted AS "lastAttempted"`,
        [childId, topicId, masteryLevel, status, correctCount ?? 0, attemptCount ?? 0]
    );

    return res.status(200).json({
        topicKey,
        ...result.rows[0],
    });
};
