import admin from 'firebase-admin';
import { env } from './env.js';

// Firebase is only initialised when real credentials are present.
// In development with placeholder values the app boots fine — auth routes
// will return 503 instead of crashing the process.

const isPlaceholder = (value) =>
    !value || value === 'placeholder' || value.includes('placeholder');

export const firebaseReady = !isPlaceholder(env.firebase.projectId) &&
                             !isPlaceholder(env.firebase.clientEmail) &&
                             !isPlaceholder(env.firebase.privateKey);

if (firebaseReady && !admin.apps.length) {
    try {
        admin.initializeApp({
            credential: admin.credential.cert({
                projectId:   env.firebase.projectId,
                privateKey:  env.firebase.privateKey,
                clientEmail: env.firebase.clientEmail,
            }),
        });
        console.log('[Firebase] Admin SDK initialised');
    } catch (err) {
        console.error('[Firebase] Failed to initialise Admin SDK:', err.message);
    }
} else if (!firebaseReady) {
    console.warn('[Firebase] Placeholder credentials detected — Firebase disabled. Auth routes return 503 until real credentials are set.');
}

export default admin;
