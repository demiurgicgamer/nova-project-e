import { v4 as uuidv4 } from 'uuid';
import { query } from '../config/database.js';
import {
    sendSessionComplete,
    sendStreakMilestone,
} from '../services/NotificationService.js';

/**
 * Sessions controller — saves completed tutoring sessions and
 * updates the child's running stats (streak, stars, current topic).
 *
 * All endpoints verify the authenticated parent owns the child.
 */

// ── Ownership guard ───────────────────────────────────────────────────────────

const getOwnedChild = async (parentId, childId) => {
    const result = await query(
        'SELECT * FROM child_profiles WHERE id = $1 AND parent_id = $2',
        [childId, parentId]
    );
    return result.rows[0] ?? null;
};

// ── Stars calculation ─────────────────────────────────────────────────────────

const calculateStars = (correctAnswers, totalQuestions, dominantEmotion) => {
    if (totalQuestions === 0) return 1; // completed but no questions = 1 star

    const accuracy = correctAnswers / totalQuestions;
    if (accuracy >= 0.9 && ['ENGAGED', 'CONFIDENT'].includes(dominantEmotion)) return 3;
    if (accuracy >= 0.7) return 2;
    return 1;
};

// ── POST /api/sessions ────────────────────────────────────────────────────────

/**
 * Saves a completed session and updates the child's profile stats atomically.
 *
 * Body: {
 *   childId, startedAt, endedAt, durationSeconds,
 *   topicsCovered[], correctAnswers, totalQuestions,
 *   dominantEmotion, emotionSummary{}, languageCode
 * }
 *
 * Response: { session, starsEarned, newTotalStars, streakDays }
 */
export const createSession = async (req, res) => {
    const parentId = req.user.sub;
    const childId  = req.params.id; // always from route — never trust body for ownership
    const {
        startedAt,
        endedAt,
        durationSeconds  = 0,
        topicsCovered    = [],
        correctAnswers   = 0,
        totalQuestions   = 0,
        dominantEmotion  = '',
        emotionSummary   = {},
        languageCode     = 'en',
    } = req.body;

    const child = await getOwnedChild(parentId, childId);
    if (!child) return res.status(404).json({ error: 'Child not found.' });

    const starsEarned = calculateStars(correctAnswers, totalQuestions, dominantEmotion);

    // ── Streak logic ──────────────────────────────────────────────────────────
    const today     = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
    const lastDate  = child.last_session_date
        ? child.last_session_date.toISOString?.().slice(0, 10) ?? String(child.last_session_date).slice(0, 10)
        : null;

    let newStreak = child.streak_days;
    if (lastDate === null) {
        newStreak = 1; // first session ever
    } else {
        const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
        if (lastDate === yesterday) newStreak = child.streak_days + 1; // consecutive day
        else if (lastDate === today)  newStreak = child.streak_days;   // already played today
        else                          newStreak = 1;                   // streak broken
    }

    // ── Current topic = last topic covered in this session ────────────────────
    const newCurrentTopic = topicsCovered.length > 0
        ? topicsCovered[topicsCovered.length - 1]
        : child.current_topic;

    const sessionId = uuidv4();

    // ── Run in a transaction — session insert + child stats update ────────────
    await query('BEGIN');
    try {
        // Insert session row
        const sessionResult = await query(
            `INSERT INTO sessions
                 (id, child_id, started_at, ended_at, duration_seconds,
                  topics_covered, correct_answers, total_questions,
                  stars_earned, emotion_summary, language_code)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
             RETURNING *`,
            [
                sessionId, childId,
                startedAt || new Date(), endedAt || new Date(),
                durationSeconds,
                topicsCovered, correctAnswers, totalQuestions,
                starsEarned, JSON.stringify(emotionSummary), languageCode,
            ]
        );

        // Update child stats
        await query(
            `UPDATE child_profiles SET
                 streak_days       = $1,
                 last_session_date = $2,
                 total_sessions    = total_sessions + 1,
                 total_stars       = total_stars + $3,
                 current_topic     = $4,
                 updated_at        = NOW()
             WHERE id = $5`,
            [newStreak, today, starsEarned, newCurrentTopic, childId]
        );

        await query('COMMIT');

        // Fetch updated child for response
        const updatedChild = await query(
            'SELECT streak_days, total_stars, total_sessions FROM child_profiles WHERE id = $1',
            [childId]
        );

        const finalStreak   = updatedChild.rows[0].streak_days;
        const topicDisplay  = topicsCovered.length > 0
            ? topicsCovered[topicsCovered.length - 1]
                .split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
            : 'Math';

        // Fire push notifications after response — non-blocking, never throw
        setImmediate(() => {
            sendSessionComplete(parentId, child.name, starsEarned, topicDisplay, finalStreak)
                .catch(e => console.warn('[sessionsController] sendSessionComplete failed:', e.message));
            sendStreakMilestone(parentId, child.name, finalStreak)
                .catch(e => console.warn('[sessionsController] sendStreakMilestone failed:', e.message));
        });

        return res.status(201).json({
            session:       sessionResult.rows[0].id,
            starsEarned,
            newTotalStars: updatedChild.rows[0].total_stars,
            streakDays:    finalStreak,
            totalSessions: updatedChild.rows[0].total_sessions,
        });

    } catch (err) {
        await query('ROLLBACK');
        throw err;
    }
};

// ── GET /api/children/:id/sessions ───────────────────────────────────────────

/**
 * Returns the most recent sessions for a child (default last 10).
 * Used by the Progress screen to show the session history list.
 *
 * Response: { sessions: [ { id, startedAt, durationSeconds,
 *                            topicsCovered, correctAnswers, totalQuestions,
 *                            starsEarned, languageCode } ] }
 */
export const getSessions = async (req, res) => {
    const parentId = req.user.sub;
    const { id: childId } = req.params;
    const limit = Math.min(parseInt(req.query.limit ?? '10', 10), 50);

    const child = await getOwnedChild(parentId, childId);
    if (!child) return res.status(404).json({ error: 'Child not found.' });

    const result = await query(
        `SELECT
             id,
             started_at       AS "startedAt",
             ended_at         AS "endedAt",
             duration_seconds AS "durationSeconds",
             topics_covered   AS "topicsCovered",
             correct_answers  AS "correctAnswers",
             total_questions  AS "totalQuestions",
             stars_earned     AS "starsEarned",
             language_code    AS "languageCode"
         FROM sessions
         WHERE child_id = $1
         ORDER BY started_at DESC
         LIMIT $2`,
        [childId, limit]
    );

    return res.status(200).json({ sessions: result.rows });
};

// ── PATCH /api/children/:id ───────────────────────────────────────────────────

/**
 * Updates mutable child profile fields.
 * Currently supports: currentTopic, languageCode, weakTopics[].
 * streak/stars/sessions are managed by createSession — not patchable directly.
 *
 * Body: { currentTopic?, languageCode?, weakTopics? }
 */
export const patchChild = async (req, res) => {
    const parentId = req.user.sub;
    const { id: childId } = req.params;
    const { currentTopic, languageCode, weakTopics } = req.body;

    const child = await getOwnedChild(parentId, childId);
    if (!child) return res.status(404).json({ error: 'Child not found.' });

    // Build update dynamically — only set fields that were provided
    const sets   = ['updated_at = NOW()'];
    const values = [];
    let   idx    = 1;

    if (currentTopic  !== undefined) { sets.push(`current_topic  = $${idx++}`); values.push(currentTopic); }
    if (languageCode  !== undefined) { sets.push(`language_code  = $${idx++}`); values.push(languageCode); }
    if (weakTopics    !== undefined) { sets.push(`weak_topics    = $${idx++}`); values.push(weakTopics); }

    if (values.length === 0)
        return res.status(400).json({ error: 'No patchable fields provided.' });

    values.push(childId);
    const result = await query(
        `UPDATE child_profiles SET ${sets.join(', ')} WHERE id = $${idx} RETURNING *`,
        values
    );

    return res.status(200).json({
        id:           result.rows[0].id,
        currentTopic: result.rows[0].current_topic,
        languageCode: result.rows[0].language_code,
        weakTopics:   result.rows[0].weak_topics,
        streakDays:   result.rows[0].streak_days,
        totalStars:   result.rows[0].total_stars,
    });
};
