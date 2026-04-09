import { spawn } from 'child_process';

// espeak-ng voice map — Phase 1: EN + FR
// Phase 2: swap for ElevenLabs voices on paid plan
const VOICE_MAP = {
    en: 'en',
    fr: 'fr',
};

// Redis TTL for cached audio. 0 = no expiry.
const CACHE_TTL_COMMON = 0;

const COMMON_PHRASE_KEYS = new Set([
    'great_job', 'lets_try_again', 'think_about_it', 'good_thinking',
    'almost_there', 'take_your_time', 'well_done', 'not_quite',
]);

/**
 * ElevenLabsTTSService — dev stub backed by espeak-ng (offline, no API key needed).
 *
 * Same public interface as the real ElevenLabs implementation.
 * Swap back to ElevenLabs when on a paid plan.
 *
 * Output: raw signed 16-bit little-endian PCM, 22050 Hz, mono
 * — matches AudioPlaybackManager.defaultSampleRate exactly.
 */
export class ElevenLabsTTSService {
    constructor(redisClient) {
        this._redis = redisClient;
    }

    async synthesizeNova(text, languageCode = 'en', phraseKey = null) {
        if (phraseKey && COMMON_PHRASE_KEYS.has(phraseKey)) {
            const cached = await this._getCached(phraseKey, languageCode);
            if (cached) {
                console.log(`[TTS] Cache hit: ${phraseKey} (${languageCode})`);
                return cached;
            }
        }

        const audio = await this._synthesize(text, languageCode);

        if (phraseKey && COMMON_PHRASE_KEYS.has(phraseKey)) {
            await this._setCache(phraseKey, languageCode, audio, CACHE_TTL_COMMON);
        }

        return audio;
    }

    async warmCache(phrasesMap) {
        let warmed = 0;
        for (const [key, texts] of Object.entries(phrasesMap)) {
            if (!COMMON_PHRASE_KEYS.has(key)) continue;
            for (const [lang, text] of Object.entries(texts)) {
                const existing = await this._getCached(key, lang);
                if (existing) continue;
                try {
                    const audio = await this._synthesize(text, lang);
                    await this._setCache(key, lang, audio, CACHE_TTL_COMMON);
                    warmed++;
                } catch (err) {
                    console.warn(`[TTS] Cache warm failed ${key}/${lang}:`, err.message);
                }
            }
        }
        console.log(`[TTS] Cache warmed — ${warmed} phrases.`);
    }

    // ── Private ───────────────────────────────────────────────────────────────

    /**
     * Generate PCM audio using espeak-ng (offline, no network).
     * Output: s16le, 22050 Hz, mono — matches Unity AudioPlaybackManager.
     */
    _synthesize(text, languageCode) {
        const voice = VOICE_MAP[languageCode] ?? 'en';

        return new Promise((resolve, reject) => {
            // espeak-ng --stdout outputs raw 16-bit PCM at 22050 Hz mono
            const proc = spawn('espeak-ng', [
                '-v', voice,
                '-s', '140',       // speed: 140 words/min (natural pace)
                '-a', '80',        // amplitude: 80% (not too loud)
                '-p', '50',        // pitch: 50 (neutral)
                '--stdout',        // write PCM to stdout
                text,
            ]);

            const chunks   = [];
            const errLines = [];

            proc.stdout.on('data', chunk => chunks.push(chunk));
            proc.stderr.on('data', chunk => errLines.push(chunk.toString()));

            proc.on('error', err => {
                console.error('[TTS] espeak-ng spawn error:', err.message);
                reject(err);
            });

            proc.on('close', code => {
                const pcm = Buffer.concat(chunks);
                if (code !== 0 && pcm.length === 0) {
                    console.error('[TTS] espeak-ng failed:', errLines.join(''));
                    reject(new Error(`espeak-ng exit ${code}`));
                    return;
                }
                console.log(`[TTS] espeak-ng OK — ${pcm.length} bytes PCM (${languageCode})`);
                resolve(pcm);
            });
        });
    }

    _cacheKey(phraseKey, languageCode) {
        return `nova:tts:${languageCode}:${phraseKey}`;
    }

    async _getCached(phraseKey, languageCode) {
        try {
            const val = await this._redis.get(this._cacheKey(phraseKey, languageCode));
            return val ? Buffer.from(val, 'base64') : null;
        } catch { return null; }
    }

    async _setCache(phraseKey, languageCode, audioBuffer, ttl) {
        try {
            const key = this._cacheKey(phraseKey, languageCode);
            const b64 = audioBuffer.toString('base64');
            ttl > 0
                ? await this._redis.set(key, b64, { EX: ttl })
                : await this._redis.set(key, b64);
        } catch (err) {
            console.warn('[TTS] Redis cache write failed:', err.message);
        }
    }
}
