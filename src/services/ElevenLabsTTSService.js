import { MsEdgeTTS, OUTPUT_FORMAT } from 'msedge-tts';
import { spawn }                    from 'child_process';

// Microsoft Edge neural TTS voices — female, Canadian accents for Phase 1
// Full voice list: https://speech.microsoft.com/portal/voicegallery
const VOICE_MAP = {
    en: 'en-CA-ClaraNeural',    // Canadian English female — warm, clear
    fr: 'fr-CA-SylvieNeural',   // Canadian French female — natural
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
     * Generate PCM audio using Microsoft Edge neural TTS (msedge-tts).
     * Returns MP3 stream → ffmpeg converts to s16le PCM at 22050 Hz mono.
     * Falls back to espeak-ng if the service is unreachable.
     */
    async _synthesize(text, languageCode) {
        const voice = VOICE_MAP[languageCode] ?? VOICE_MAP.en;
        try {
            const mp3 = await this._msEdgeTTS(text, voice);
            const pcm = await this._mp3ToPcm(mp3);
            console.log(`[TTS] msedge-tts OK — ${pcm.length} bytes PCM (${voice})`);
            return pcm;
        } catch (err) {
            console.warn(`[TTS] msedge-tts failed (${err.message}) — falling back to espeak-ng`);
            return this._espeakFallback(text, languageCode);
        }
    }

    /** Fetch MP3 audio from Microsoft Edge Read Aloud service. */
    _msEdgeTTS(text, voice) {
        return new Promise(async (resolve, reject) => {
            try {
                const tts = new MsEdgeTTS();
                await tts.setMetadata(voice, OUTPUT_FORMAT.AUDIO_24KHZ_96KBITRATE_MONO_MP3);

                const { audioStream } = tts.toStream(text);
                const chunks = [];

                audioStream.on('data',  chunk => chunks.push(chunk));
                audioStream.on('close', ()    => resolve(Buffer.concat(chunks)));
                audioStream.on('error', err   => reject(err));
            } catch (err) {
                reject(err);
            }
        });
    }

    /** Convert MP3 buffer → raw s16le PCM at 22050 Hz mono via ffmpeg. */
    _mp3ToPcm(mp3Buffer) {
        return new Promise((resolve, reject) => {
            const ff = spawn('ffmpeg', [
                '-i', 'pipe:0',    // read from stdin
                '-f', 's16le',     // raw PCM
                '-ar', '22050',    // matches Unity AudioPlaybackManager.defaultSampleRate
                '-ac', '1',        // mono
                'pipe:1',          // write to stdout
            ]);

            const chunks = [];
            const errs   = [];

            ff.stdout.on('data', c => chunks.push(c));
            ff.stderr.on('data', c => errs.push(c.toString()));
            ff.on('error', reject);
            ff.on('close', code => {
                const pcm = Buffer.concat(chunks);
                if (code !== 0 && pcm.length === 0)
                    reject(new Error(`ffmpeg exit ${code}: ${errs.slice(-2).join('')}`));
                else
                    resolve(pcm);
            });

            ff.stdin.write(mp3Buffer);
            ff.stdin.end();
        });
    }

    /** espeak-ng fallback — used when edge-tts is unavailable. */
    _espeakFallback(text, languageCode) {
        const voice = languageCode === 'fr' ? 'fr' : 'en';
        return new Promise((resolve, reject) => {
            const proc = spawn('espeak-ng', ['-v', voice, '-s', '140', '-a', '80', '--stdout', text]);
            const chunks = [];
            proc.stdout.on('data', c => chunks.push(c));
            proc.on('error', reject);
            proc.on('close', () => resolve(Buffer.concat(chunks)));
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
