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
        console.log(`[TTS] _synthesize — lang=${languageCode}, voice=${voice}`);
        console.log(`[TTS] Text (${text.length} chars): "${text.substring(0, 80)}"`);

        // ── Try msedge-tts first ─────────────────────────────────────────────
        try {
            console.log(`[TTS] Calling msedge-tts...`);
            const mp3 = await this._msEdgeTTS(text, voice);
            console.log(`[TTS] msedge-tts returned ${mp3.length} bytes`);

            if (mp3.length === 0) {
                throw new Error('msedge-tts returned empty buffer — stream closed with no data');
            }
            // Log first 4 bytes to confirm it is a valid MP3 (should start with FF FB, FF F3, or ID3)
            const mp3Header = mp3.slice(0, 4).toString('hex');
            console.log(`[TTS] MP3 header: 0x${mp3Header} (valid MP3 starts with ffFB/ffF3/ID3)`);

            console.log(`[TTS] Piping MP3 → ffmpeg for PCM conversion...`);
            const pcm = await this._mp3ToPcm(mp3);
            console.log(`[TTS] ffmpeg output: ${pcm.length} bytes PCM (~${(pcm.length / 2 / 22050).toFixed(2)}s @ 22050Hz)`);

            if (pcm.length === 0) {
                throw new Error('ffmpeg produced 0 bytes — MP3 may be corrupt');
            }

            // Scan the whole buffer for non-zero bytes (silent PCM = TTS produced nothing useful)
            const sampleSize = Math.min(pcm.length, 8000);
            let nonZeroCount = 0;
            for (let i = 0; i < sampleSize; i++) {
                if (pcm[i] !== 0) nonZeroCount++;
            }
            const silentPct = ((1 - nonZeroCount / sampleSize) * 100).toFixed(1);
            console.log(`[TTS] msedge PCM sanity: ${nonZeroCount}/${sampleSize} bytes non-zero, silent=${silentPct}%`);

            if (nonZeroCount === 0) {
                throw new Error(`msedge-tts PCM is pure silence (${pcm.length} bytes all-zero) — Edge TTS service may be unreachable from Docker`);
            }

            return pcm;

        } catch (err) {
            console.warn(`[TTS] msedge-tts pipeline FAILED: ${err.message}`);
            console.warn(`[TTS] Falling back to espeak-ng...`);
        }

        // ── espeak-ng fallback ───────────────────────────────────────────────
        try {
            const pcm = await this._espeakFallback(text, languageCode);
            console.log(`[TTS] espeak-ng fallback OK — ${pcm.length} bytes PCM`);

            const espeakSample = Math.min(pcm.length, 8000);
            let nonZeroCount = 0;
            for (let i = 0; i < espeakSample; i++) {
                if (pcm[i] !== 0) nonZeroCount++;
            }
            const silentPct = ((1 - nonZeroCount / espeakSample) * 100).toFixed(1);
            console.log(`[TTS] espeak PCM sanity: ${nonZeroCount}/${espeakSample} bytes non-zero, silent=${silentPct}%`);

            if (pcm.length === 0) {
                throw new Error('espeak-ng produced 0 bytes — voice data may not be installed');
            }
            if (nonZeroCount === 0) {
                throw new Error(`espeak-ng PCM is pure silence (${pcm.length} bytes all-zero) — voice data issue or Docker audio not configured`);
            }
            return pcm;
        } catch (err) {
            console.error(`[TTS] espeak-ng fallback ALSO FAILED: ${err.message}`);
            throw err;
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

                audioStream.on('data',  (chunk) => {
                    chunks.push(chunk);
                    // Log every chunk so we know data is flowing
                    console.log(`[TTS] msedge chunk received: ${chunk.length} bytes (total so far: ${chunks.reduce((s,c)=>s+c.length,0)})`);
                });
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
            ff.on('error', (err) => {
                console.error(`[TTS] ffmpeg spawn error: ${err.message}`);
                reject(err);
            });
            ff.on('close', code => {
                const pcm = Buffer.concat(chunks);
                // Always log ffmpeg stderr for diagnosis
                if (errs.length > 0) {
                    const ffmpegLog = errs.join('').split('\n').filter(l => l.includes('Error') || l.includes('error') || l.includes('size=')).join('\n');
                    if (ffmpegLog) console.log(`[TTS] ffmpeg log: ${ffmpegLog}`);
                }
                if (code !== 0 && pcm.length === 0)
                    reject(new Error(`ffmpeg exit ${code}: ${errs.slice(-2).join('')}`));
                else
                    resolve(pcm);
            });

            ff.stdin.write(mp3Buffer);
            ff.stdin.end();
        });
    }

    /**
     * espeak-ng fallback — used when edge-tts is unavailable.
     * espeak-ng --stdout emits a RIFF WAV file; pipe through ffmpeg to get
     * raw s16le PCM at 22050 Hz so AudioPlaybackManager can consume it directly.
     */
    _espeakFallback(text, languageCode) {
        const voice = languageCode === 'fr' ? 'fr' : 'en';
        console.log(`[TTS] espeak-ng: voice=${voice}, text="${text.substring(0, 60)}"`);

        return new Promise((resolve, reject) => {
            const espeak = spawn('espeak-ng', ['-v', voice, '-s', '140', '-a', '100', '--stdout', text]);
            const ff     = spawn('ffmpeg', [
                '-i', 'pipe:0',
                '-f', 's16le',
                '-ar', '22050',
                '-ac', '1',
                'pipe:1',
            ]);

            const chunks   = [];
            const errs     = [];
            const eSpkErrs = [];

            espeak.stdout.pipe(ff.stdin);
            espeak.stderr.on('data', c => {
                eSpkErrs.push(c.toString());
                console.warn(`[TTS] espeak stderr: ${c.toString().trim()}`);
            });
            espeak.on('error', (err) => {
                console.error(`[TTS] espeak-ng spawn error: ${err.message} — is espeak-ng installed?`);
                ff.stdin.destroy(err);
            });
            espeak.on('close', (code) => {
                console.log(`[TTS] espeak-ng exited with code ${code}`);
            });

            ff.stdout.on('data', c => chunks.push(c));
            ff.stderr.on('data', c => errs.push(c.toString()));
            ff.on('error', reject);
            ff.on('close', code => {
                const pcm = Buffer.concat(chunks);
                console.log(`[TTS] espeak+ffmpeg → ${pcm.length} bytes PCM, exit code ${code}`);
                if (code !== 0 && pcm.length === 0)
                    reject(new Error(`espeak/ffmpeg exit ${code}: ${errs.slice(-2).join('')}`));
                else
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
