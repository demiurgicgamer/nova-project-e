import { initializeApp, cert, applicationDefault, getApps } from 'firebase-admin/app';
import { existsSync, readFileSync } from 'fs';
import { resolve } from 'path';

/**
 * Firebase Admin SDK initialisation — modular API (firebase-admin v10+).
 *
 * Credential resolution order:
 *   1. service-account-key.json in project root  (recommended for local dev)
 *   2. FIREBASE_* env vars                        (recommended for Docker / CI)
 *   3. Application Default Credentials            (works on GCP / Cloud Run)
 *
 * The deprecated "Database Secrets" from the Firebase console are NOT supported.
 * Download a proper service account key:
 *   Firebase Console → Project Settings → Service Accounts → Generate new private key
 *
 * firebaseReady is exported so middleware can return 503 instead of crashing
 * when no credentials are configured yet.
 */

const SERVICE_ACCOUNT_PATH = resolve(process.cwd(), 'service-account-key.json');

// ── Credential detection ──────────────────────────────────────────────────────

function loadFromFile() {
    if (!existsSync(SERVICE_ACCOUNT_PATH)) return null;
    try {
        const raw = JSON.parse(readFileSync(SERVICE_ACCOUNT_PATH, 'utf8'));
        // Validate it looks like a real service account key, not a database secret
        if (!raw.private_key || !raw.client_email || !raw.project_id) {
            console.warn('[Firebase] service-account-key.json is missing required fields ' +
                         '(private_key, client_email, project_id). ' +
                         'Download a fresh key from Firebase Console → Service Accounts.');
            return null;
        }
        return cert(raw);
    } catch (err) {
        console.error('[Firebase] Failed to parse service-account-key.json:', err.message);
        return null;
    }
}

function loadFromEnv() {
    const projectId   = process.env.FIREBASE_PROJECT_ID;
    const clientEmail = process.env.FIREBASE_CLIENT_EMAIL;
    // .env stores newlines as literal \n — restore them
    const privateKey  = process.env.FIREBASE_PRIVATE_KEY?.replace(/\\n/g, '\n');

    const isPlaceholder = (v) => !v || v === 'placeholder' || v.includes('placeholder');

    if (isPlaceholder(projectId) || isPlaceholder(clientEmail) || isPlaceholder(privateKey)) {
        return null;
    }

    // Reject database secrets — they're short hex strings, not PEM keys
    if (!privateKey.includes('BEGIN PRIVATE KEY')) {
        console.warn('[Firebase] FIREBASE_PRIVATE_KEY looks like a database secret (legacy). ' +
                     'These are deprecated. Download a service account key from ' +
                     'Firebase Console → Project Settings → Service Accounts → Generate new private key.');
        return null;
    }

    return cert({ projectId, clientEmail, privateKey });
}

// ── Initialisation ────────────────────────────────────────────────────────────

let _credential = loadFromFile() ?? loadFromEnv();
let _credSource = 'none';

if (!_credential) {
    // Try Application Default Credentials as a last resort (GCP / Cloud Run)
    try {
        _credential = applicationDefault();
        _credSource = 'adc';
    } catch {
        _credSource = 'none';
    }
} else {
    _credSource = existsSync(SERVICE_ACCOUNT_PATH) ? 'file' : 'env';
}

export let firebaseReady = false;

if (_credSource !== 'none' && !getApps().length) {
    try {
        initializeApp({ credential: _credential });
        firebaseReady = true;
        console.log(`[Firebase] Admin SDK initialised (credential source: ${_credSource})`);
    } catch (err) {
        console.error('[Firebase] initializeApp failed:', err.message);
    }
} else if (getApps().length) {
    // Already initialised (e.g. hot-reload in development)
    firebaseReady = true;
} else {
    console.warn(
        '[Firebase] No credentials found — auth routes will return 503.\n' +
        '  Option A (local dev): place service-account-key.json in the project root.\n' +
        '  Option B (Docker/CI): set FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY in .env.\n' +
        '  Download keys: Firebase Console → Project Settings → Service Accounts → Generate new private key.'
    );
}
