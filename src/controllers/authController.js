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

const sanitizeParent = ({ id, email, subscription_active, consent_date, created_at }) =>
    ({ id, email, subscriptionActive: subscription_active, consentDate: consent_date, createdAt: created_at });

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

    if (!consentGranted) {
        return res.status(400).json({ error: 'Parental consent is required to register.' });
    }

    if (!email) {
        return res.status(400).json({ error: 'Firebase account must have an email address.' });
    }

    // Check for duplicate
    const existing = await query(
        'SELECT id FROM parent_profiles WHERE firebase_uid = $1 OR email = $2',
        [uid, email]
    );
    if (existing.rows.length > 0) {
        return res.status(409).json({ error: 'An account with this email already exists.' });
    }

    const id = uuidv4();
    const result = await query(
        `INSERT INTO parent_profiles (id, email, firebase_uid, subscription_active, consent_date)
         VALUES ($1, $2, $3, false, NOW())
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
