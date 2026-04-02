import { query } from '../config/database.js';

/**
 * POST /api/consent
 *
 * Records parental consent with a timestamp.
 * Idempotent — calling it again just updates the consent_date.
 *
 * Body: { consentGranted: true, appVersion: string, consentDate: ISO8601 string }
 * Auth: requireAuth (backend JWT)
 */
export const recordConsent = async (req, res) => {
    const { consentGranted, appVersion, consentDate } = req.body;
    const parentId = req.user.sub;

    if (!consentGranted) {
        return res.status(400).json({ error: 'consentGranted must be true.' });
    }

    // Validate parentId exists
    const parent = await query(
        'SELECT id FROM parent_profiles WHERE id = $1',
        [parentId]
    );

    if (parent.rows.length === 0) {
        return res.status(404).json({ error: 'Parent profile not found.' });
    }

    // Update consent_date (upsert pattern — safe to call multiple times)
    await query(
        `UPDATE parent_profiles
         SET consent_date = $1, updated_at = NOW()
         WHERE id = $2`,
        [consentDate || new Date().toISOString(), parentId]
    );

    console.log(`[Consent] Recorded for parent ${parentId} | app v${appVersion}`);

    return res.status(200).json({ success: true });
};
