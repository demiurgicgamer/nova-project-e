import { WebSocketServer, WebSocket } from 'ws';
import { DeepgramSTTService } from './DeepgramSTTService.js';
import { ElevenLabsTTSService } from './ElevenLabsTTSService.js';
import redisClient from '../config/redis.js';

const HEARTBEAT_INTERVAL_MS = 20_000; // server-side ping every 20s
const HEARTBEAT_TIMEOUT_MS  = 10_000; // close connection if no pong within 10s

/**
 * SessionWebSocketServer
 *
 * Handles the persistent WebSocket connections from Unity clients.
 * One connection = one tutoring session.
 *
 * Path: ws://host/session/{sessionId}
 *
 * Inbound events (Unity → Backend):
 *   { type: 'session_start', childId, sessionId, languageCode, grade }
 *   { type: 'audio_chunk',   data: <base64 PCM>, sampleRate }
 *   { type: 'session_end',   sessionId }
 *   { type: 'ping' }
 *
 * Outbound events (Backend → Unity):
 *   { type: 'nova_speaking',     text, audioBase64 }
 *   { type: 'animation_trigger', emotion }
 *   { type: 'whiteboard_update', problem, steps[], languageCode }
 *   { type: 'session_progress',  topicsCompleted, accuracy }
 *   { type: 'session_summary',   starsEarned, streakDays, totalSessions }
 *   { type: 'pong' }
 *   { type: 'error',             message }
 */
export class SessionWebSocketServer {
    constructor() {
        this._wss      = null;
        this._sessions = new Map(); // sessionId → SessionState
        this._tts      = new ElevenLabsTTSService(redisClient);
    }

    /**
     * Attach to an existing HTTP(S) server.
     * Call this after app.listen() in app.js.
     * @param {import('http').Server} httpServer
     */
    attach(httpServer) {
        // No path filter — handle routing manually so /session/{id} subpaths work
        this._wss = new WebSocketServer({ server: httpServer });

        this._wss.on('connection', (ws, req) => {
            // Only accept connections to /session/{sessionId}
            const match = req.url?.match(/^\/session\/(.+)$/);
            if (!match) {
                ws.close(1008, 'Invalid path. Use /session/{sessionId}');
                return;
            }
            const sessionId = match[1];

            console.log(`[WSS] Client connected — session: ${sessionId}`);
            this._initSession(ws, sessionId);
        });

        this._wss.on('error', (err) => {
            console.error('[WSS] Server error:', err);
        });

        console.log('[WSS] WebSocket server attached.');
    }

    // ── Session lifecycle ─────────────────────────────────────────────────────

    _initSession(ws, sessionId) {
        const stt = new DeepgramSTTService();

        const state = {
            ws,
            sessionId,
            childId:      null,
            languageCode: 'en',
            grade:        6,
            stt,
            pingTimer:    null,
            pongReceived: true,
        };

        this._sessions.set(sessionId, state);

        // Wire STT transcripts → agent (placeholder) → TTS → Unity
        stt.on('transcript', (result) => this._onTranscript(state, result));
        stt.on('error', (err) => {
            console.error(`[WSS] STT error (${sessionId}):`, err);
            this._send(ws, { type: 'error', message: 'Speech recognition error. Please try again.' });
        });

        ws.on('message', (data) => this._onMessage(state, data));
        ws.on('close',   ()     => this._onClose(state));
        ws.on('error',   (err)  => console.error(`[WSS] Socket error (${sessionId}):`, err));
        ws.on('pong',    ()     => { state.pongReceived = true; });

        this._startHeartbeat(state);
    }

    _onClose(state) {
        console.log(`[WSS] Client disconnected — session: ${state.sessionId}`);
        clearInterval(state.pingTimer);
        state.stt.closeAll();
        this._sessions.delete(state.sessionId);
    }

    // ── Inbound message handling ──────────────────────────────────────────────

    _onMessage(state, rawData) {
        let msg;
        try { msg = JSON.parse(rawData.toString()); }
        catch { console.warn('[WSS] Non-JSON message received.'); return; }

        switch (msg.type) {
            case 'session_start':
                this._handleSessionStart(state, msg);
                break;

            case 'audio_chunk':
                this._handleAudioChunk(state, msg);
                break;

            case 'session_end':
                this._handleSessionEnd(state);
                break;

            case 'ping':
                this._send(state.ws, { type: 'pong' });
                break;

            // Editor-only test shortcut: skip STT, inject fake transcript directly
            case 'test_transcript':
                if (msg.text) {
                    console.log(`[WSS] Test transcript injected: "${msg.text}"`);
                    this._onTranscript(state, { text: msg.text, isFinal: true, languageCode: state.languageCode });
                }
                break;

            default:
                console.warn(`[WSS] Unknown message type: ${msg.type}`);
        }
    }

    _handleSessionStart(state, msg) {
        state.childId      = msg.childId;
        state.languageCode = msg.languageCode ?? 'en';
        state.grade        = msg.grade ?? 6;

        console.log(`[WSS] Session started — child: ${state.childId}, lang: ${state.languageCode}, grade: ${state.grade}`);

        // Trigger Ms. Nova's intro animation while agent warms up
        this._send(state.ws, { type: 'animation_trigger', emotion: 'Idle' });

        // TODO (Day 28): initialise Claude agent here
        // AgentOrchestrator.startSession(state.sessionId, { childId, languageCode, grade });
    }

    _handleAudioChunk(state, msg) {
        if (!msg.data) return;

        let buffer;
        try { buffer = Buffer.from(msg.data, 'base64'); }
        catch { return; }

        state.chunkCount = (state.chunkCount ?? 0) + 1;
        // Log every 50 chunks (~5 seconds) to confirm audio is flowing
        if (state.chunkCount % 50 === 1)
            console.log(`[WSS] Audio chunk #${state.chunkCount} — ${buffer.length}B (session: ${state.sessionId})`);

        state.stt.processAudioChunk(buffer, state.languageCode);
    }

    _handleSessionEnd(state) {
        console.log(`[WSS] Session end requested — session: ${state.sessionId}`);
        state.stt.closeAll();

        // TODO (Day 37): flush session data to DB via SessionSaveService
        // Then send session_summary back to Unity
        this._send(state.ws, {
            type:         'session_summary',
            starsEarned:  1,   // placeholder until agent integration
            streakDays:   0,
            totalSessions: 0,
        });
    }

    // ── STT → Agent → TTS pipeline ────────────────────────────────────────────

    async _onTranscript(state, { text, isFinal, languageCode }) {
        if (!isFinal || !text) return;

        console.log(`[WSS] Transcript (${languageCode}): "${text}"`);

        // TODO (Day 28): route transcript through Claude agent
        // const agentResponse = await AgentOrchestrator.sendMessage(state.sessionId, text);
        // For now, echo a placeholder so the voice pipeline can be tested end-to-end.
        const agentResponse = `I heard you say: ${text}. Let me think about that.`;

        await this._speakAsNova(state, agentResponse);
    }

    async _speakAsNova(state, text) {
        if (state.ws.readyState !== WebSocket.OPEN) return;

        try {
            this._send(state.ws, { type: 'animation_trigger', emotion: 'Talking' });

            console.log(`[WSS] TTS start — "${text.substring(0, 40)}..."`);
            const audioBuffer = await this._tts.synthesizeNova(text, state.languageCode);
            console.log(`[WSS] TTS done — ${audioBuffer.length} bytes PCM`);

            const audioBase64 = audioBuffer.toString('base64');
            console.log(`[WSS] Sending nova_speaking — base64 length: ${audioBase64.length}`);

            this._send(state.ws, {
                type:        'nova_speaking',
                text,
                audioBase64,
            });

            console.log(`[WSS] nova_speaking sent OK`);
        } catch (err) {
            console.error(`[WSS] TTS failed (${state.sessionId}):`, err.message, err.stack);
            this._send(state.ws, { type: 'animation_trigger', emotion: 'Idle' });
            this._send(state.ws, { type: 'error', message: 'Voice synthesis unavailable.' });
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    _send(ws, payload) {
        if (ws.readyState !== WebSocket.OPEN) return;
        ws.send(JSON.stringify(payload));
    }

    _startHeartbeat(state) {
        state.pongReceived = true;
        state.pingTimer = setInterval(() => {
            if (!state.pongReceived) {
                console.warn(`[WSS] No pong received — closing stale session: ${state.sessionId}`);
                state.ws.terminate();
                return;
            }
            state.pongReceived = false;
            if (state.ws.readyState === WebSocket.OPEN) {
                state.ws.ping();
            }
        }, HEARTBEAT_INTERVAL_MS);
    }
}
