import { v4 as uuidv4 } from 'uuid';
import bcrypt from 'bcryptjs';
import { query } from '../config/database.js';
import { signAccessToken, signRefreshToken } from '../middleware/auth.js';

const BCRYPT_ROUNDS = 12;

// ── Helpers ───────────────────────────────────────────────────────────────────

const buildTokenPair = (parent) => {
    const payload = { sub: parent.id, email: parent.email, role: 'parent' };
    return {
        accessToken:  signAccessToken(payload),
        refreshToken: signRefreshToken(payload),
    };
};

const VALID_REGIONS = ['canada', 'quebec', 'us', 'india', 'global'];

const sanitizeParent = ({ id, email, subscription_active, consent_date, curriculum_region, created_at }) =>
    ({ id, email, subscriptionActive: subscription_active, consentDate: consent_date, curriculumRegion: curriculum_region ?? 'canada', createdAt: created_at });

// ── Controllers ───────────────────────────────────────────────────────────────

/**
 * POST /api/auth/register
 *
 * Flow: Unity authenticates with Firebase → sends Firebase ID token →
 * backend verifies token → creates parent_profile row → returns JWT pair.
 *
 * Body: { consentGranted: true }
 * Header: Authorization: Bearer <firebase_id_token>
 */
export const register = async (req, res) => {
    const { consentGranted } = req.body;
    const { uid, email } = req.firebaseUser; // set by verifyFirebaseToken middleware

    if (!email) {
        return res.status(400).json({ error: 'Firebase account must have an email address.' });
    }

    // Check for duplicate — return existing tokens instead of erroring,
    // so auto-register on login fallback is idempotent.
    const existing = await query(
        'SELECT * FROM parent_profiles WHERE firebase_uid = $1 OR email = $2',
        [uid, email]
    );
    if (existing.rows.length > 0) {
        const parent = existing.rows[0];
        const tokens = buildTokenPair(parent);
        return res.status(200).json({
            parent: sanitizeParent(parent),
            ...tokens,
        });
    }

    const id = uuidv4();
    // Only stamp consent_date if the client explicitly passed consentGranted: true.
    // Unity's ConsentController sends true after the user taps "I Agree".
    // The login-fallback auto-register sends false — consent screen will show next.
    const consentDate = consentGranted === true ? 'NOW()' : 'NULL';

    const result = await query(
        `INSERT INTO parent_profiles (id, email, firebase_uid, subscription_active, consent_date)
         VALUES ($1, $2, $3, false, ${consentDate})
         RETURNING *`,
        [id, email, uid]
    );

    const parent = result.rows[0];
    const tokens = buildTokenPair(parent);

    return res.status(201).json({
        parent: sanitizeParent(parent),
        ...tokens,
    });
};

/**
 * POST /api/auth/login
 *
 * Flow: Unity re-authenticates with Firebase → sends fresh Firebase ID token →
 * backend verifies → finds existing parent row → returns new JWT pair.
 *
 * Header: Authorization: Bearer <firebase_id_token>
 */
export const login = async (req, res) => {
    const { uid, email } = req.firebaseUser;

    const result = await query(
        'SELECT * FROM parent_profiles WHERE firebase_uid = $1',
        [uid]
    );

    if (result.rows.length === 0) {
        return res.status(404).json({ error: 'No account found. Please register first.' });
    }

    const parent = result.rows[0];
    const tokens = buildTokenPair(parent);

    return res.status(200).json({
        parent: sanitizeParent(parent),
        ...tokens,
    });
};

/**
 * GET /api/auth/profile
 * Returns the current parent's profile including curriculum_region.
 * Header: Authorization: Bearer <access_token>
 */
export const getProfile = async (req, res) => {
    const result = await query('SELECT * FROM parent_profiles WHERE id = $1', [req.user.sub]);
    if (result.rows.length === 0) {
        return res.status(404).json({ error: 'Profile not found.' });
    }
    return res.status(200).json({ parent: sanitizeParent(result.rows[0]) });
};

/**
 * PATCH /api/auth/profile
 * Updates curriculum_region for the current parent.
 * Body: { curriculumRegion: 'canada' | 'quebec' | 'india' | 'global' }
 * Header: Authorization: Bearer <access_token>
 */
export const updateProfile = async (req, res) => {
    const { curriculumRegion } = req.body;

    if (!curriculumRegion || !VALID_REGIONS.includes(curriculumRegion)) {
        return res.status(400).json({
            error: `curriculumRegion must be one of: ${VALID_REGIONS.join(', ')}`
        });
    }

    const result = await query(
        `UPDATE parent_profiles
            SET curriculum_region = $1
          WHERE id = $2
          RETURNING *`,
        [curriculumRegion, req.user.sub]
    );

    if (result.rows.length === 0) {
        return res.status(404).json({ error: 'Profile not found.' });
    }

    return res.status(200).json({ parent: sanitizeParent(result.rows[0]) });
};

/**
 * POST /api/auth/refresh
 *
 * Exchanges a valid refresh token for a new access token.
 * Body: { refreshToken: string }
 */
export const refresh = async (req, res) => {
    // req.user is set by requireRefreshToken middleware
    const { sub, email } = req.user;

    // Verify parent still exists before issuing new token
    const result = await query('SELECT id, email FROM parent_profiles WHERE id = $1', [sub]);
    if (result.rows.length === 0) {
        return res.status(401).json({ error: 'Account no longer exists.' });
    }

    const accessToken = signAccessToken({ sub, email, role: 'parent' });
    return res.status(200).json({ accessToken });
};
