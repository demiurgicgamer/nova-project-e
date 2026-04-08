import { EventEmitter } from 'events';
import { createClient, LiveTranscriptionEvents } from '@deepgram/sdk';
import { env } from '../config/env.js';

// Language code → Deepgram model config
// Phase 1: English (Canadian) + French (Canadian)
// Phase 2: Hindi, Arabic, Spanish
const LANGUAGE_CONFIG = {
    en: { language: 'en-US', model: 'nova-2' },
    fr: { language: 'fr-CA', model: 'nova-2' },
};

/**
 * DeepgramSTTService
 *
 * Maintains one live Deepgram WebSocket connection per language code.
 * Accepts raw PCM audio chunks and emits 'transcript' events.
 *
 * Usage:
 *   const stt = new DeepgramSTTService();
 *   stt.on('transcript', ({ text, isFinal, confidence, languageCode }) => { ... });
 *   await stt.processAudioChunk(buffer, 'en');
 *   stt.close('en');
 *   stt.closeAll();
 *
 * Events emitted:
 *   'transcript'  — { text, isFinal, confidence, languageCode }
 *   'error'       — { error, languageCode }
 */
export class DeepgramSTTService extends EventEmitter {
    constructor() {
        super();
        this._client      = createClient(env.apis.deepgram);
        this._connections = new Map(); // languageCode → { connection, ready, buffer[] }
    }

    // ── Public API ────────────────────────────────────────────────────────────

    /**
     * Send an audio chunk to Deepgram for the given language.
     * Opens a new connection automatically if one doesn't exist yet.
     * @param {Buffer} audioBuffer  Raw PCM audio bytes (16-bit, mono)
     * @param {string} languageCode 'en' or 'fr'
     */
    async processAudioChunk(audioBuffer, languageCode = 'en') {
        const conn = await this._getOrCreateConnection(languageCode);

        if (conn.ready) {
            conn.connection.send(audioBuffer);
        } else {
            // Buffer until connection is ready
            conn.buffer.push(audioBuffer);
        }
    }

    /**
     * Gracefully close a specific language connection.
     */
    close(languageCode) {
        const conn = this._connections.get(languageCode);
        if (conn) {
            conn.connection.finish();
            this._connections.delete(languageCode);
        }
    }

    /**
     * Close all open connections (call when session ends).
     */
    closeAll() {
        for (const lang of this._connections.keys()) {
            this.close(lang);
        }
    }

    // ── Private ───────────────────────────────────────────────────────────────

    async _getOrCreateConnection(languageCode) {
        if (this._connections.has(languageCode)) {
            return this._connections.get(languageCode);
        }

        const config = LANGUAGE_CONFIG[languageCode] ?? LANGUAGE_CONFIG.en;

        const state = { connection: null, ready: false, buffer: [] };
        this._connections.set(languageCode, state);

        const connection = this._client.listen.live({
            model:            config.model,
            language:         config.language,
            encoding:         'linear16',   // 16-bit PCM
            sample_rate:      16000,         // matches AgoraConfig.CaptureSampleRate
            channels:         1,
            punctuate:        true,
            interim_results:  true,
            endpointing:      300,           // ms of silence before utterance end
            utterance_end_ms: 1000,
        });

        state.connection = connection;

        connection.on(LiveTranscriptionEvents.Open, () => {
            console.log(`[Deepgram] Connection open (${languageCode})`);
            state.ready = true;
            // Flush buffered chunks
            for (const chunk of state.buffer) {
                connection.send(chunk);
            }
            state.buffer = [];
        });

        connection.on(LiveTranscriptionEvents.Transcript, (data) => {
            const alt = data?.channel?.alternatives?.[0];
            if (!alt || !alt.transcript) return;

            const isFinal = data.is_final ?? false;

            // Skip empty interim results
            if (!isFinal && !alt.transcript.trim()) return;

            this.emit('transcript', {
                text:         alt.transcript.trim(),
                isFinal,
                confidence:   alt.confidence ?? 0,
                languageCode,
                words:        alt.words ?? [],
            });
        });

        connection.on(LiveTranscriptionEvents.Error, (err) => {
            console.error(`[Deepgram] Error (${languageCode}):`, err);
            this.emit('error', { error: err, languageCode });
        });

        connection.on(LiveTranscriptionEvents.Close, () => {
            console.log(`[Deepgram] Connection closed (${languageCode})`);
            state.ready = false;

            // Auto-reconnect if connection was not intentionally closed
            if (this._connections.has(languageCode)) {
                console.log(`[Deepgram] Reconnecting (${languageCode})...`);
                this._connections.delete(languageCode);
                // Next processAudioChunk call will re-open
            }
        });

        return state;
    }
}
