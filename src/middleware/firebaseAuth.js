import { getAuth } from 'firebase-admin/auth';
import { firebaseReady } from '../config/firebase.js';

/**
 * Verifies a Firebase ID token sent from the Unity client.
 * Uses the modular Firebase Admin SDK (firebase-admin/auth).
 *
 * Attaches decoded Firebase claims to req.firebaseUser:
 *   req.firebaseUser.uid       — Firebase UID
 *   req.firebaseUser.email     — verified email (if email/password auth)
 *   req.firebaseUser.firebase  — sign-in provider details
 *
 * Unity sends: Authorization: Bearer <firebase_id_token>
 * Called before register/login — no backend JWT exists yet at that point.
 *
 * Returns 503 when Firebase credentials are not configured.
 */
export const verifyFirebaseToken = async (req, res, next) => {
    if (!firebaseReady) {
        return res.status(503).json({
            error: 'Firebase not configured. See server logs for credential setup instructions.',
        });
    }

    const header = req.headers.authorization;
    if (!header?.startsWith('Bearer ')) {
        return res.status(401).json({ error: 'Missing or invalid Authorization header.' });
    }

    const idToken = header.slice(7);

    try {
        // getAuth() uses the already-initialised default app
        req.firebaseUser = await getAuth().verifyIdToken(idToken, /* checkRevoked */ true);
        next();
    } catch (err) {
        const code = err.code ?? '';
        console.error('[Firebase] Token verification failed:', err.message);

        if (code === 'auth/id-token-revoked') {
            return res.status(401).json({ error: 'Token has been revoked. Please sign in again.' });
        }
        if (code === 'auth/id-token-expired') {
            return res.status(401).json({ error: 'Token has expired. Please sign in again.' });
        }

        return res.status(401).json({ error: 'Invalid Firebase token.' });
    }
};
