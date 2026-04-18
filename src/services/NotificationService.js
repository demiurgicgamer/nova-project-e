import { getMessaging } from 'firebase-admin/messaging';
import { query } from '../config/database.js';
import { firebaseReady } from '../config/firebase.js';

/**
 * NotificationService — sends Firebase Cloud Messaging push notifications.
 *
 * Three trigger types (MVP):
 *   1. Session complete  — sent right after a session is saved
 *   2. Streak milestone  — sent when streak hits 3 / 7 / 14 / 30 days
 *   3. Daily reminder    — sent by an external cron (see sendDailyReminders)
 *
 * FCM token lifecycle:
 *   - Unity registers the token via POST /api/notifications/token on every login.
 *   - Token is stored in parent_profiles.fcm_token (migration 006).
 *   - Stale tokens (FCM returns UNREGISTERED) are cleared automatically.
 *
 * All methods are safe to call even when Firebase isn't configured — they log
 * a warning and return without throwing.
 */

// ── Streak milestone thresholds ───────────────────────────────────────────────
const STREAK_MILESTONES = new Set([3, 7, 14, 30, 60, 100]);

// ── Core send helper ──────────────────────────────────────────────────────────

/**
 * Sends a single FCM notification to one device token.
 * Returns true on success, false on any failure (stale token cleared from DB).
 *
 * @param {string} token   - FCM registration token
 * @param {string} title   - Notification title
 * @param {string} body    - Notification body
 * @param {Object} data    - Optional key/value data payload (all values must be strings)
 * @param {string} parentId - Parent UUID — used to clear stale tokens
 */
async function sendToToken(token, title, body, data = {}, parentId = null) {
    if (!firebaseReady) {
        console.warn('[NotificationService] Firebase not initialised — skipping push.');
        return false;
    }

    if (!token) {
        console.warn('[NotificationService] sendToToken called with empty token — skipping.');
        return false;
    }

    const message = {
        token,
        notification: { title, body },
        data: {
            ...data,
            // Ensure all values are strings (FCM requirement)
            timestamp: String(Date.now()),
        },
        android: {
            priority: 'high',
            notification: {
                channelId: 'nova_notifications',
                icon:      'ic_notification',
                color:     '#7F77DD',
                sound:     'default',
            },
        },
    };

    try {
        const response = await getMessaging().send(message);
        console.log(`[NotificationService] Sent → ${response} (${title})`);
        return true;
    } catch (err) {
        const code = err?.errorInfo?.code ?? err?.code ?? '';

        // Stale token — device uninstalled or token rotated — remove from DB
        if (
            code === 'messaging/registration-token-not-registered' ||
            code === 'messaging/invalid-registration-token'
        ) {
            console.warn(`[NotificationService] Stale token cleared for parent ${parentId ?? 'unknown'}`);
            if (parentId) {
                await query(
                    'UPDATE parent_profiles SET fcm_token = NULL WHERE id = $1',
                    [parentId]
                ).catch(() => {}); // Non-fatal
            }
        } else {
            console.error(`[NotificationService] FCM send failed: ${err.message}`);
        }
        return false;
    }
}

// ── Token management ──────────────────────────────────────────────────────────

/**
 * Saves (or updates) the FCM token for a parent.
 * Called by POST /api/notifications/token.
 *
 * @param {string} parentId
 * @param {string} fcmToken
 */
export async function registerToken(parentId, fcmToken) {
    if (!parentId || !fcmToken) return;

    await query(
        `UPDATE parent_profiles
            SET fcm_token  = $1,
                updated_at = NOW()
          WHERE id = $2`,
        [fcmToken, parentId]
    );

    console.log(`[NotificationService] FCM token registered for parent ${parentId}`);
}

// ── Notification triggers ─────────────────────────────────────────────────────

/**
 * Fires after a session is saved successfully.
 * Shows parents a quick summary: child name, stars earned, topic.
 *
 * @param {string} parentId
 * @param {string} childName
 * @param {number} starsEarned   1 | 2 | 3
 * @param {string} topicDisplay  Human-readable topic name (e.g. "Algebra Basics")
 * @param {number} streakDays    Current streak after this session
 */
export async function sendSessionComplete(parentId, childName, starsEarned, topicDisplay, streakDays) {
    const tokenResult = await query(
        'SELECT fcm_token FROM parent_profiles WHERE id = $1',
        [parentId]
    ).catch(() => null);

    const token = tokenResult?.rows?.[0]?.fcm_token;
    if (!token) return;

    const starStr = '⭐'.repeat(Math.max(1, Math.min(3, starsEarned)));
    const title   = `${childName} finished a session! ${starStr}`;
    const body    = streakDays > 1
        ? `${topicDisplay} · 🔥 ${streakDays}-day streak`
        : `Great start on ${topicDisplay}!`;

    await sendToToken(token, title, body, {
        type:    'session_complete',
        childId: String(parentId), // deep-link data
        stars:   String(starsEarned),
    }, parentId);
}

/**
 * Fires when a streak hits a milestone (3, 7, 14, 30, 60, 100 days).
 * Call this AFTER sendSessionComplete (share the same token fetch).
 *
 * @param {string} parentId
 * @param {string} childName
 * @param {number} streakDays
 */
export async function sendStreakMilestone(parentId, childName, streakDays) {
    if (!STREAK_MILESTONES.has(streakDays)) return;

    const tokenResult = await query(
        'SELECT fcm_token FROM parent_profiles WHERE id = $1',
        [parentId]
    ).catch(() => null);

    const token = tokenResult?.rows?.[0]?.fcm_token;
    if (!token) return;

    const title = `🔥 ${streakDays}-Day Streak!`;
    const body  = `${childName} has been learning for ${streakDays} days in a row. Amazing!`;

    await sendToToken(token, title, body, {
        type:       'streak_milestone',
        streakDays: String(streakDays),
    }, parentId);
}

/**
 * Daily reminder — call from an external cron job (e.g. node-cron at 17:00 local).
 * Sends to ALL parents whose children haven't had a session today.
 *
 * This method is intentionally NOT wired into any HTTP route — invoke directly
 * from a scheduled task:
 *
 *   import { sendDailyReminders } from './services/NotificationService.js';
 *   cron.schedule('0 17 * * *', sendDailyReminders);
 */
export async function sendDailyReminders() {
    if (!firebaseReady) return;

    // Find parents whose active child hasn't played today
    const today = new Date().toISOString().slice(0, 10);

    let rows;
    try {
        const result = await query(
            `SELECT DISTINCT
                 pp.id        AS parent_id,
                 pp.fcm_token,
                 cp.name      AS child_name
             FROM parent_profiles pp
             JOIN child_profiles  cp ON cp.parent_id = pp.id
             WHERE pp.fcm_token IS NOT NULL
               AND (
                   cp.last_session_date IS NULL
                   OR cp.last_session_date < $1::date
               )
             ORDER BY pp.id`,
            [today]
        );
        rows = result.rows;
    } catch (err) {
        console.error('[NotificationService] sendDailyReminders DB query failed:', err.message);
        return;
    }

    console.log(`[NotificationService] Sending daily reminders to ${rows.length} parents`);

    for (const row of rows) {
        const title = `${row.child_name} hasn't learned today yet! 📚`;
        const body  = 'A 10-minute Nova session keeps the streak alive. Tap to start!';

        await sendToToken(row.fcm_token, title, body, {
            type: 'daily_reminder',
        }, row.parent_id);

        // Small delay to avoid FCM rate limits on large batches
        await new Promise(r => setTimeout(r, 50));
    }
}
