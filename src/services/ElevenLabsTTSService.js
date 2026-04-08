import { ElevenLabsClient } from '@elevenlabs/elevenlabs-js';
import { createClient } from 'redis';
import { env } from '../config/env.js';

// Voice IDs — set these after selecting/cloning voices in the ElevenLabs dashboard.
// Format: { languageCode: 'voice_id_string' }
// Placeholder values below — replace with real IDs before Month 2 testing.
const NOVA_VOICE_IDS = {
    en: process.env.ELEVENLABS_VOICE_ID_EN || 'placeholder_en_voice_id',
    hi: process.env.ELEVENLABS_VOICE_ID_HI || 'placeholder_hi_voice_id',
};

const MODEL_ID = 'eleven_multilingual_v2';

// Redis TTL for cached audio (seconds). 0 = no expiry (permanent for common phrases).
const CACHE_TTL_COMMON  = 0;
const CACHE_TTL_DYNAMIC = 0; // dynamic TTS is not cached — only common phrases

// Short phrases Ms. Nova says frequently — these are pre-cached in Redis on first call.
const COMMON_PHRASE_KEYS = new Set([
    'great_job', 'lets_try_again', 'think_about_it', 'good_thinking',
    'almost_there', 'take_your_time', 'well_done', 'not_quite',
]);

/**
 * ElevenLabsTTSService
 *
 * Converts text to speech using ElevenLabs eleven_multilingual_v2.
 * Caches frequently-used short phrases in Redis to reduce latency and API cost.
 *
 * Usage:
 *   const tts = new ElevenLabsTTSService(redisClient);
 *   const audioBuffer = await tts.synthesizeNova(text, 'en');
 *   // audioBuffer is a Buffer of raw MP3 bytes — send to Unity via WebSocket
 */
export class ElevenLabsTTSService {
    /**
     * @param {import('redis').RedisClientType} redisClient  Connected Redis client
     */
    constructor(redisClient) {
        this._client = new ElevenLabsClient({ apiKey: env.apis.elevenlabs });
        this._redis  = redisClient;
    }

    // ── Public API ────────────────────────────────────────────────────────────

    /**
     * Synthesize text as Ms. Nova's voice.
     * Returns a Buffer of MP3 audio bytes.
     *
     * @param {string} text          Text to speak
     * @param {string} languageCode  'en' or 'hi'
     * @param {string} [phraseKey]   Optional cache key for common phrases (e.g. 'great_job')
     * @returns {Promise<Buffer>}
     */
    async synthesizeNova(text, languageCode = 'en', phraseKey = null) {
        const voiceId = NOVA_VOICE_IDS[languageCode] ?? NOVA_VOICE_IDS.en;

        // Try cache for common phrases
        if (phraseKey && COMMON_PHRASE_KEYS.has(phraseKey)) {
            const cached = await this._getCached(phraseKey, languageCode);
            if (cached) {
                console.log(`[ElevenLabs] Cache hit: ${phraseKey} (${languageCode})`);
                return cached;
            }
        }

        // Call ElevenLabs API
        const audio = await this._callAPI(text, voiceId);

        // Cache common phrases permanently
        if (phraseKey && COMMON_PHRASE_KEYS.has(phraseKey)) {
            await this._setCache(phraseKey, languageCode, audio, CACHE_TTL_COMMON);
        }

        return audio;
    }

    /**
     * Pre-warm the Redis cache for all common phrases in both languages.
     * Call this at session server startup to minimize first-session latency.
     * Requires COMMON_PHRASE_TEXTS to be populated in your env or config.
     *
     * @param {Object} phrasesMap  { phraseKey: { en: '...', hi: '...' } }
     */
    async warmCache(phrasesMap) {
        let warmed = 0;
        for (const [key, texts] of Object.entries(phrasesMap)) {
            if (!COMMON_PHRASE_KEYS.has(key)) continue;
            for (const [lang, text] of Object.entries(texts)) {
                const existing = await this._getCached(key, lang);
                if (existing) continue;
                try {
                    const audio = await this._callAPI(text, NOVA_VOICE_IDS[lang] ?? NOVA_VOICE_IDS.en);
                    await this._setCache(key, lang, audio, CACHE_TTL_COMMON);
                    warmed++;
                    console.log(`[ElevenLabs] Cached: ${key} (${lang})`);
                } catch (err) {
                    console.warn(`[ElevenLabs] Cache warm failed for ${key}/${lang}:`, err.message);
                }
            }
        }
        console.log(`[ElevenLabs] Cache warm complete — ${warmed} phrases cached.`);
    }

    // ── Private ───────────────────────────────────────────────────────────────

    async _callAPI(text, voiceId) {
        const stream = await this._client.textToSpeech.convert(voiceId, {
            text,
            model_id:         MODEL_ID,
            output_format:    'mp3_22050_32',   // 22050 Hz, 32kbps — good quality, low size
            voice_settings: {
                stability:        0.55,
                similarity_boost: 0.75,
                style:            0.30,         // slight expressiveness for a teacher
                use_speaker_boost: true,
            },
        });

        // Collect streamed chunks into a single Buffer
        const chunks = [];
        for await (const chunk of stream) {
            chunks.push(chunk);
        }
        return Buffer.concat(chunks);
    }

    _cacheKey(phraseKey, languageCode) {
        return `nova:tts:${languageCode}:${phraseKey}`;
    }

    async _getCached(phraseKey, languageCode) {
        try {
            const val = await this._redis.get(this._cacheKey(phraseKey, languageCode));
            return val ? Buffer.from(val, 'base64') : null;
        } catch {
            return null;
        }
    }

    async _setCache(phraseKey, languageCode, audioBuffer, ttl) {
        try {
            const key = this._cacheKey(phraseKey, languageCode);
            const b64 = audioBuffer.toString('base64');
            if (ttl > 0) {
                await this._redis.set(key, b64, { EX: ttl });
            } else {
                await this._redis.set(key, b64); // no expiry
            }
        } catch (err) {
            console.warn('[ElevenLabs] Redis cache write failed:', err.message);
        }
    }
}
