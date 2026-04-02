import admin, { firebaseReady } from '../config/firebase.js';

/**
 * Verifies a Firebase ID token sent from the Unity client.
 * Attaches the decoded Firebase claims to req.firebaseUser.
 *
 * Unity sends: Authorization: Bearer <firebase_id_token>
 * Called before register/login — no backend JWT exists yet at that point.
 *
 * Returns 503 when Firebase credentials are not yet configured (Month 1 dev).
 */
export const verifyFirebaseToken = async (req, res, next) => {
    if (!firebaseReady) {
        return res.status(503).json({
            error: 'Firebase not configured. Set real credentials in .env to use auth endpoints.',
        });
    }

    const header = req.headers.authorization;
    if (!header?.startsWith('Bearer ')) {
        return res.status(401).json({ error: 'Missing or invalid Authorization header' });
    }

    const idToken = header.slice(7);
    try {
        req.firebaseUser = await admin.auth().verifyIdToken(idToken);
        next();
    } catch (err) {
        console.error('[Firebase] Token verification failed:', err.message);
        return res.status(401).json({ error: 'Invalid or expired Firebase token' });
    }
};
