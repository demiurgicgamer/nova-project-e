import { WebSocketServer, WebSocket } from 'ws';
import { DeepgramSTTService } from './DeepgramSTTService.js';
import { ElevenLabsTTSService } from './ElevenLabsTTSService.js';
import redisClient from '../config/redis.js';
import { query } from '../config/database.js';
// Native fetch available in Node 20 — no import needed
const AGENT_BASE_URL = process.env.AGENT_BASE_URL ?? 'http://nova-agent:3001';

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
 *   { type: 'session_start',  childId, sessionId, languageCode, grade, currentTopic }
 *   { type: 'audio_chunk',    data: <base64 PCM>, sampleRate }
 *   { type: 'session_end',    sessionId }
 *   { type: 'answer_submit',  isCorrect: bool }   — tap answer result from question card
 *   { type: 'ping' }
 *
 * Outbound events (Backend → Unity):
 *   { type: 'nova_speaking',     text, audioBase64 }
 *   { type: 'animation_trigger', emotion }
 *   { type: 'whiteboard_update', problem, steps[], languageCode }
 *   { type: 'question_display',  text, choices[], correctIndex }  — MC question card
 *   { type: 'chunk_phase',       phase }           — HUD dot strip progress
 *   { type: 'session_progress',  topicsCompleted, accuracy }
 *   { type: 'session_summary',   starsEarned, streakDays, totalSessions, correctAnswers, totalQuestions }
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
            micActive:        false,  // true while Unity mic is recording; agent ignores transcripts until false
            novaProcessing:   false,  // true while agent+TTS pipeline is running; drops duplicate transcripts
            cardGeneration:   0,      // incremented on every turn; delayed card sends check this before firing
            novaSpeaking:     false,  // true from nova_speaking send until playback_complete arrives from Unity
            _playbackResolve: null,   // Promise resolver — called by _onPlaybackComplete
            pendingSilenceText: null, // question text waiting for silence timer after playback ends
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
        clearTimeout(state.transcriptTimeoutId);
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

            // App going to background or being killed mid-session.
            // Save a resume checkpoint without ending the session so the child
            // continues from the right place when they reopen the app.
            case 'session_pause':
                this._saveSessionCheckpoint(state);
                break;

            case 'ping':
                this._send(state.ws, { type: 'pong' });
                break;

            // Mic activated — PTT pressed. Mark active so audio chunks are forwarded to STT.
            case 'mic_start':
                console.log(`[WSS] mic_start received (${state.sessionId})`);
                state.micActive = true;
                this._cancelSilenceTimer(state);   // child is responding — cancel hint timer
                break;

            // Mic stopped — mark inactive first, then flush Deepgram so final transcript fires
            // after micActive is false and will be routed to the agent
            case 'mic_stop':
                console.log(`[WSS] mic_stop received — finalising Deepgram (${state.sessionId})`);
                state.micActive = false;
                clearTimeout(state.transcriptTimeoutId);
                state.stt.closeAll();   // finish() → Deepgram sends pending final transcript
                // Process transcript buffered during mic-active window (endpointing fires before mic_stop)
                if (state.pendingTranscript) {
                    const t = state.pendingTranscript;
                    state.pendingTranscript = null;
                    console.log(`[WSS] Processing buffered transcript: "${t.text.substring(0, 60)}"`);
                    this._onTranscript(state, { text: t.text, isFinal: true, languageCode: t.languageCode });
                } else {
                    // If no transcript arrives within 5 seconds, Deepgram got silence (or Editor mic
                    // is muted on Windows). Send processing_complete so Unity returns to Listening
                    // instead of waiting out the full 45-second session-level timeout.
                    state.transcriptTimeoutId = setTimeout(() => {
                        if (!state.novaProcessing) {
                            console.log(`[WSS] No transcript after mic_stop (5s) — sending processing_complete to recover (${state.sessionId})`);
                            this._send(state.ws, { type: 'processing_complete' });
                        }
                    }, 5000);
                }
                break;

            // Editor-only test shortcut: skip STT, inject fake transcript directly
            case 'test_transcript':
                if (msg.text) {
                    console.log(`[WSS] Test transcript injected: "${msg.text}"`);
                    this._onTranscript(state, { text: msg.text, isFinal: true, languageCode: state.languageCode });
                }
                break;

            // Unity AudioPlaybackManager finished playing all queued clips.
            // Resolve the _waitForPlayback promise so the question card can be sent now.
            case 'playback_complete':
                this._onPlaybackComplete(state);
                break;

            // Tap answer result from Unity's question card (Option B local evaluation)
            case 'answer_submit':
                this._cancelSilenceTimer(state);   // child tapped an answer — cancel hint timer
                this._handleAnswerSubmit(state, msg);
                break;

            default:
                console.warn(`[WSS] Unknown message type: ${msg.type}`);
        }
    }

    _handleSessionStart(state, msg) {
        state.childId       = msg.childId;
        state.childName     = msg.childName    ?? 'friend';
        state.languageCode  = msg.languageCode ?? 'en';
        state.grade         = msg.grade        ?? 6;
        state.weakTopics    = msg.weakTopics   ?? [];
        state.selectedTopic = msg.currentTopic ?? null;   // topic_key chosen in TopicPicker
        state.startTime     = Date.now();

        console.log(`[WSS] Session started — child: ${state.childId}, lang: ${state.languageCode}, grade: ${state.grade}`);

        // Trigger Ms. Nova's intro animation while agent warms up
        this._send(state.ws, { type: 'animation_trigger', emotion: 'Idle' });

        // Notify agent service to initialise session context in Redis,
        // then immediately generate and speak the welcome + PTT guide
        this._agentSessionStart(state)
            .then(() => this._agentSessionIntro(state))
            .catch(err =>
                console.warn(`[WSS] Agent session init/intro failed (non-fatal): ${err.message}`)
            );
    }

    _handleAudioChunk(state, msg) {
        if (!msg.data) return;

        let buffer;
        try { buffer = Buffer.from(msg.data, 'base64'); }
        catch { return; }

        state.chunkCount = (state.chunkCount ?? 0) + 1;
        // NOTE: do NOT set state.micActive here. micActive is set only by mic_start
        // (PTT pressed) and cleared by mic_stop (PTT released). Agora fires audio
        // frames continuously — even when the mic is muted — so setting micActive on
        // every chunk causes a race condition: the flag is reset to true before
        // Deepgram can fire its final transcript after mic_stop, permanently buffering
        // every transcript and keeping Unity stuck in Processing state.
        // Log every 50 chunks (~5 seconds) to confirm audio is flowing
        if (state.chunkCount % 50 === 1)
            console.log(`[WSS] Audio chunk #${state.chunkCount} — ${buffer.length}B (session: ${state.sessionId}, micActive: ${state.micActive})`);

        // Only forward audio to Deepgram when PTT is actively pressed
        if (state.micActive) {
            state.stt.processAudioChunk(buffer, state.languageCode);
        }
    }

    async _handleSessionEnd(state) {
        console.log(`[WSS] Session end requested — session: ${state.sessionId}`);
        state.sessionEnding = true;   // prevent _continueLesson from firing after session closes
        state.stt.closeAll();

        // Tell the Python agent to clean up Redis state and return session accuracy stats.
        // Stats are used to populate session_summary so Unity can compute mastery updates.
        let agentCorrect = 0, agentTotal = 0;
        if (state.sessionId) {
            try {
                const agentRes = await fetch(`${AGENT_BASE_URL}/session/end`, {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    JSON.stringify({ session_id: state.sessionId }),
                    signal:  AbortSignal.timeout(5000),
                });
                const agentData = await agentRes.json();
                agentCorrect = agentData.session_correct ?? 0;
                agentTotal   = agentData.session_total   ?? 0;
                console.log(`[WSS] Agent session cleaned up (${state.sessionId}) — correct: ${agentCorrect}/${agentTotal}`);
            } catch (err) {
                console.warn(`[WSS] Agent /session/end failed (non-fatal): ${err.message}`);
            }
        }

        // Query real child stats from DB.
        // The Unity REST call (POST /api/children/:id/sessions) runs before session_end
        // is sent, so DB values are already updated with the correct streak + totals.
        let starsEarned = 1, streakDays = 0, totalSessions = 0;
        if (state.childId) {
            try {
                const result = await query(
                    'SELECT streak_days, total_sessions FROM child_profiles WHERE id = $1',
                    [state.childId]
                );
                if (result.rows[0]) {
                    streakDays    = result.rows[0].streak_days    ?? 0;
                    totalSessions = result.rows[0].total_sessions ?? 0;
                }
                console.log(`[WSS] session_summary — child: ${state.childId}, streak: ${streakDays}, total: ${totalSessions}`);
            } catch (err) {
                console.warn(`[WSS] session_summary DB query failed (non-fatal): ${err.message}`);
            }
        }

        this._send(state.ws, {
            type:           'session_summary',
            starsEarned,
            streakDays,
            totalSessions,
            correctAnswers: agentCorrect,
            totalQuestions: agentTotal,
        });
    }

    // ── STT → Agent → TTS pipeline ────────────────────────────────────────────

    async _onTranscript(state, { text, isFinal, languageCode }) {
        if (!isFinal || !text) return;

        // Clear the no-transcript fallback timer — a real transcript arrived
        clearTimeout(state.transcriptTimeoutId);

        // If mic is still active, buffer the transcript instead of ignoring it.
        // Deepgram may fire a final result via endpointing before the user presses Stop.
        // We'll process it immediately when mic_stop arrives.
        if (state.micActive) {
            console.log(`[WSS] Transcript buffered (mic active): "${text.substring(0, 60)}"`);
            state.pendingTranscript = { text, languageCode };
            return;
        }

        // Deepgram's finish() can fire multiple isFinal events (pending utterances + flush).
        // Drop them if a response is already in flight — one response per mic session.
        if (state.novaProcessing) {
            console.log(`[WSS] Transcript dropped (Nova processing): "${text.substring(0, 60)}"`);
            return;
        }

        console.log(`[WSS] Transcript (${languageCode}): "${text}"`);
        this._cancelSilenceTimer(state);   // real transcript arrived — cancel hint timer
        state.novaProcessing = true;

        // Claim a generation token so any older pending delayed card sends are
        // invalidated.  We check this again after the delay before sending cards.
        const myGen = ++state.cardGeneration;

        try {
            // Route transcript through Claude agent
            const { responseText, animationEmotion, whiteboardProblem, whiteboardSteps, languageCode,
                    questionDisplay, chunkPhase }
                = await this._callAgent(state, text);

            // Drive animator before TTS so the animation starts immediately
            if (animationEmotion && animationEmotion !== 'Talking') {
                this._send(state.ws, { type: 'animation_trigger', emotion: animationEmotion });
            }

            // HUD dot strip can update immediately — it's subtle UI, not problem content.
            if (chunkPhase) {
                this._send(state.ws, { type: 'chunk_phase', phase: chunkPhase });
            }

            // Speak FIRST — Nova describes the problem aloud.
            // Both the whiteboard card and the MC answer card are held until Nova
            // finishes speaking so neither appears while she is still talking.
            const playbackMs = await this._speakAsNova(state, responseText);

            // Wait for Unity to confirm playback is done before revealing visual cards.
            // _waitForPlayback resolves on playback_complete from Unity, or times out
            // at 1.5× the estimated duration + 1s as a safe fallback.
            if (whiteboardProblem || questionDisplay) {
                await this._waitForPlayback(state, playbackMs);
            }

            // Stale check — if a newer turn started while we were awaiting, skip
            // sending these cards; the newer turn will send its own.
            if (state.cardGeneration !== myGen) {
                console.log(`[WSS] question_display skipped — stale gen ${myGen} vs current ${state.cardGeneration}`);
                return;
            }

            if (whiteboardProblem) {
                console.log(`[WSS] whiteboard_update — problem: "${whiteboardProblem.substring(0, 50)}…", steps: ${whiteboardSteps.length}`);
                this._send(state.ws, {
                    type:         'whiteboard_update',
                    problem:      whiteboardProblem,
                    steps:        whiteboardSteps,
                    languageCode: languageCode,
                });
            }

            if (questionDisplay) {
                console.log(`[WSS] question_display — choices: ${questionDisplay.choices?.length ?? 0}`);
                this._send(state.ws, {
                    type:         'question_display',
                    text:         questionDisplay.text         ?? '',
                    choices:      questionDisplay.choices      ?? [],
                    correctIndex: questionDisplay.correct_index ?? 0,
                });
            }
        } finally {
            state.novaProcessing = false;
        }
    }

    // ── Agent HTTP helpers ────────────────────────────────────────────────────

    /**
     * POST /session/start to the Python agent service.
     * Initialises Redis session context so history is persisted correctly.
     */
    async _agentSessionStart(state) {
        const body = {
            session_id: state.sessionId,
            child_profile: {
                child_id:    state.childId      ?? 'unknown',
                name:        state.childName    ?? 'friend',
                grade:       state.grade        ?? 6,
                language:    state.languageCode ?? 'en',
                weak_topics: state.weakTopics   ?? [],
            },
            selected_topic: state.selectedTopic ?? null,
        };

        const res = await fetch(`${AGENT_BASE_URL}/session/start`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(body),
            signal:  AbortSignal.timeout(5000),
        });

        if (!res.ok) {
            throw new Error(`Agent /session/start → HTTP ${res.status}`);
        }
        console.log(`[WSS] Agent session initialised (${state.sessionId})`);
    }

    /**
     * POST /session/intro to generate Ms. Nova's opening welcome + PTT guide.
     * Called once after session_start is acknowledged.
     */
    async _agentSessionIntro(state) {
        const body = {
            session_id: state.sessionId,
            child_profile: {
                child_id:    state.childId      ?? 'unknown',
                name:        state.childName    ?? 'friend',
                grade:       state.grade        ?? 6,
                language:    state.languageCode ?? 'en',
                weak_topics: state.weakTopics   ?? [],
            },
            selected_topic: state.selectedTopic ?? null,
        };

        const res = await fetch(`${AGENT_BASE_URL}/session/intro`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(body),
            signal:  AbortSignal.timeout(15000),
        });

        if (!res.ok) {
            throw new Error(`Agent /session/intro → HTTP ${res.status}`);
        }

        const data = await res.json();
        const isResume = !!(data.whiteboard_problem);
        console.log(
            `[WSS] Intro — "${data.response_text?.substring(0, 80)}" | ` +
            `emotion: ${data.animation_emotion} | resume_problem: ${isResume ? 'YES' : 'no'}`
        );

        if (!data.response_text) {
            console.error(`[WSS] Intro response_text empty (${state.sessionId}) — skipping TTS`);
            this._send(state.ws, { type: 'processing_complete' });
            return;
        }

        if (data.animation_emotion && data.animation_emotion !== 'Talking') {
            this._send(state.ws, { type: 'animation_trigger', emotion: data.animation_emotion });
        }

        // Do NOT forward data.chunk_phase during the intro.
        // On a resumed session the Python agent returns the stored Redis chunk state
        // (e.g. "consolidate" = 100%) which would fill the progress bar immediately
        // before any work is done this session.  The bar always starts at 0% ("intro")
        // and only advances once _continueLesson fires after the first answer.
        this._send(state.ws, { type: 'chunk_phase', phase: 'intro' });

        // Claim generation token before the delay so any later turn can invalidate this send.
        const introGen = ++state.cardGeneration;

        // Speak first — whiteboard and MC card are both held until Nova finishes
        // talking so neither card appears while she is still speaking.
        const introPlaybackMs = await this._speakAsNova(state, data.response_text);

        // Wait for Unity to confirm playback is done before revealing visual cards.
        if (data.whiteboard_problem || data.question_display) {
            await this._waitForPlayback(state, introPlaybackMs);
        }

        // Stale check — abort if a newer turn started during our await.
        if (state.cardGeneration !== introGen) {
            console.log(`[WSS] Intro question_display skipped — stale gen ${introGen} vs current ${state.cardGeneration}`);
            return;
        }

        if (data.whiteboard_problem) {
            console.log(`[WSS] Intro whiteboard — "${data.whiteboard_problem.substring(0, 60)}…"`);
            this._send(state.ws, {
                type:         'whiteboard_update',
                problem:      data.whiteboard_problem,
                steps:        data.whiteboard_steps ?? [],
                languageCode: state.languageCode,
            });
        }

        if (data.question_display) {
            console.log(`[WSS] Intro question_display — choices: ${data.question_display.choices?.length ?? 0}`);
            this._send(state.ws, {
                type:         'question_display',
                text:         data.question_display.text          ?? '',
                choices:      data.question_display.choices       ?? [],
                correctIndex: data.question_display.correct_index ?? 0,
            });
        }
    }

    /**
     * Save a mid-session checkpoint without ending the session.
     * Called when Unity sends session_pause (app goes to background / is killed).
     * Ensures resume works even if the child never pressed "End Session".
     */
    async _saveSessionCheckpoint(state) {
        if (!state.sessionId) return;
        try {
            await fetch(`${AGENT_BASE_URL}/session/checkpoint`, {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ session_id: state.sessionId }),
                signal:  AbortSignal.timeout(4000),
            });
            console.log(`[WSS] Mid-session checkpoint saved (${state.sessionId})`);
        } catch (err) {
            console.warn(`[WSS] Checkpoint save failed (non-fatal): ${err.message}`);
        }
    }

    /**
     * POST /chat to the Python agent service.
     * Returns Ms. Nova's response text and animation hint.
     * Falls back to a safe echo response if the agent is unavailable.
     */
    async _callAgent(state, text) {
        const body = {
            session_id:    state.sessionId,
            text,
            emotion_state: state.emotionState ?? 'NEUTRAL',
            child_profile: {
                child_id:    state.childId   ?? 'unknown',
                name:        state.childName ?? 'friend',
                grade:       state.grade     ?? 6,
                language:    state.languageCode ?? 'en',
                weak_topics: state.weakTopics   ?? [],
            },
        };

        try {
            const res = await fetch(`${AGENT_BASE_URL}/chat`, {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify(body),
                signal:  AbortSignal.timeout(15000),   // 15s — Claude can be slow on first call
            });

            if (!res.ok) {
                const errText = await res.text();
                throw new Error(`Agent /chat → HTTP ${res.status}: ${errText}`);
            }

            const data = await res.json();
            const hasWhiteboard = data.whiteboard_problem && data.whiteboard_problem.trim().length > 0;
            console.log(
                `[WSS] Agent response (${state.sessionId}): "${data.response_text?.substring(0, 60)}..." ` +
                `| anim: ${data.animation_emotion} | whiteboard: ${hasWhiteboard ? 'YES' : 'no'}`
            );
            return {
                responseText:      data.response_text     ?? 'Let me think about that.',
                animationEmotion:  data.animation_emotion ?? 'Talking',
                whiteboardProblem: data.whiteboard_problem ?? null,
                whiteboardSteps:   data.whiteboard_steps  ?? [],
                languageCode:      state.languageCode     ?? 'en',
                questionDisplay:   data.question_display  ?? null,
                chunkPhase:        data.chunk_phase       ?? 'chunk_a',
            };
        } catch (err) {
            console.error(`[WSS] Agent call failed (${state.sessionId}): ${err.message}`);
            // Graceful fallback — pipeline keeps working even if agent is down
            return {
                responseText:     "That's interesting! Can you tell me more about what you're working on?",
                animationEmotion: 'Talking',
                whiteboardProblem: null,
                whiteboardSteps:  [],
                languageCode:     state.languageCode ?? 'en',
                questionDisplay:  null,
                chunkPhase:       'chunk_a',
            };
        }
    }

    /**
     * Synthesize TTS and send nova_speaking to Unity.
     *
     * Sets state.novaSpeaking = true immediately; the flag is cleared in
     * _onPlaybackComplete when Unity confirms audio has finished playing.
     * Silence timer is also started there for accurate timing.
     *
     * Returns the estimated playback duration in ms (s16le PCM @ 22050 Hz)
     * so _waitForPlayback can use it as a timeout fallback.
     */
    async _speakAsNova(state, text) {
        if (state.ws.readyState !== WebSocket.OPEN) return 0;

        // Guard: never send empty/null text to TTS — it would produce silent audio
        if (!text || !text.trim()) {
            console.warn(`[WSS] _speakAsNova called with empty text (session: ${state.sessionId}) — skipping TTS`);
            this._send(state.ws, { type: 'processing_complete' });
            return 0;
        }

        try {
            this._send(state.ws, { type: 'animation_trigger', emotion: 'Talking' });

            console.log(`[WSS] TTS start — "${text.substring(0, 40)}..."`);
            const audioBuffer = await this._tts.synthesizeNova(text, state.languageCode);
            console.log(`[WSS] TTS done — ${audioBuffer.length} bytes PCM`);

            const audioBase64 = audioBuffer.toString('base64');
            console.log(`[WSS] Sending nova_speaking — base64 length: ${audioBase64.length}`);

            // Mark Nova as speaking BEFORE sending so callers that check novaSpeaking
            // immediately after this call see the correct state.
            state.novaSpeaking = true;

            // If the response contains a question, store the text so _onPlaybackComplete
            // can start the silence timer at the exact moment audio finishes on Unity.
            state.pendingSilenceText = text.includes('?') ? text : null;

            this._send(state.ws, { type: 'nova_speaking', text, audioBase64 });
            console.log(`[WSS] nova_speaking sent OK`);

            // Estimated duration: s16le PCM at 22050 Hz (msedge-tts output rate)
            // Used as a timeout fallback in _waitForPlayback; actual completion
            // is signalled by the playback_complete event from Unity.
            const durationMs = Math.ceil((audioBuffer.length / 2 / 22050) * 1000);
            console.log(`[WSS] Estimated playback: ${durationMs}ms (~${(durationMs/1000).toFixed(1)}s)`);

            return durationMs;
        } catch (err) {
            console.error(`[WSS] TTS failed (${state.sessionId}):`, err.message, err.stack);
            this._send(state.ws, { type: 'animation_trigger', emotion: 'Idle' });
            this._send(state.ws, { type: 'processing_complete' });
            state.novaSpeaking = false;
            return 0;
        }
    }

    /**
     * Called when Unity's AudioPlaybackManager fires OnPlaybackComplete.
     * This is the authoritative signal that Nova has stopped speaking.
     *
     * Actions:
     *   1. Clear novaSpeaking flag
     *   2. Resolve any _waitForPlayback promise so card-send logic can proceed
     *   3. Start the silence timer if Nova just asked a question
     */
    _onPlaybackComplete(state) {
        console.log(`[WSS] playback_complete received from Unity (${state.sessionId})`);
        state.novaSpeaking = false;

        // Resolve the waiting card-send promise
        if (state._playbackResolve) {
            const fn = state._playbackResolve;
            state._playbackResolve = null;
            fn();
        }

        // Start silence timer now that audio has ACTUALLY ended on the device
        if (state.pendingSilenceText) {
            const questionText = state.pendingSilenceText;
            state.pendingSilenceText = null;
            if (!state.micActive && !state.novaProcessing && !state.sessionEnding) {
                this._startSilenceTimer(state, questionText);
            }
        }
    }

    /**
     * Wait until Unity confirms playback is complete via playback_complete event.
     * Falls back to a timeout of (estimatedMs × 1.5 + 1 s) in case the event
     * is lost (network blip, Unity crash, very short clip that completes before
     * the Promise is registered).
     *
     * @param {object} state       - session state
     * @param {number} estimatedMs - estimated duration from PCM buffer size
     */
    _waitForPlayback(state, estimatedMs) {
        // If TTS failed (estimatedMs === 0) or Nova is already not speaking, continue immediately
        if (estimatedMs <= 0 || !state.novaSpeaking) return Promise.resolve();

        return new Promise(resolve => {
            // Safety timeout: 1.5× estimated duration + 1 s
            const timeoutMs = Math.ceil(estimatedMs * 1.5) + 1000;
            const timer = setTimeout(() => {
                if (state._playbackResolve === resolve) {
                    state._playbackResolve = null;
                    state.novaSpeaking = false;
                    console.warn(`[WSS] _waitForPlayback timeout after ${timeoutMs}ms — continuing without playback_complete (${state.sessionId})`);
                }
                resolve();
            }, timeoutMs);

            state._playbackResolve = () => {
                clearTimeout(timer);
                resolve();
            };
        });
    }

    /** Simple promise-based delay helper. */
    _delay(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    // ── Silence timeout ───────────────────────────────────────────────────────
    // After Nova asks a question (text ends with "?"), we start a 7-second timer.
    // If the child presses PTT or taps an answer before it fires, the timer is
    // cancelled.  If it fires, Nova offers a gentle hint to prompt the child.

    _startSilenceTimer(state, questionText) {
        this._cancelSilenceTimer(state);
        const SILENCE_MS = 7_000;
        state.silenceTimerId = setTimeout(async () => {
            state.silenceTimerId = null;
            if (state.ws.readyState !== WebSocket.OPEN) return;
            if (state.sessionEnding) return;
            if (state.novaProcessing) return;  // don't fire if pipeline already busy

            console.log(`[WSS] Silence timeout — offering hint (session: ${state.sessionId})`);
            await this._offerHint(state, questionText);
        }, SILENCE_MS);
    }

    _cancelSilenceTimer(state) {
        if (state.silenceTimerId) {
            clearTimeout(state.silenceTimerId);
            state.silenceTimerId = null;
        }
    }

    /**
     * Called when the child hasn't responded after 7 seconds.
     * Calls the agent with a special hint-request message so Nova offers
     * a gentle nudge using the actual question context from session history.
     */
    async _offerHint(state, lastQuestion) {
        if (state.novaProcessing) return;
        // Set novaProcessing synchronously BEFORE any await so a concurrent call
        // that checks the flag immediately after us sees it as busy.
        state.novaProcessing = true;

        // Invalidate any pending delayed card sends from _continueLesson or
        // _onTranscript — their stale question_display must not fire during the hint.
        ++state.cardGeneration;

        const hintTrigger = state.languageCode === 'fr'
            ? "Je ne sais pas trop par où commencer."
            : "I'm not sure where to start.";

        try {
            const { responseText, animationEmotion } = await this._callAgent(state, hintTrigger);

            if (animationEmotion && animationEmotion !== 'Talking') {
                this._send(state.ws, { type: 'animation_trigger', emotion: animationEmotion });
            }
            await this._speakAsNova(state, responseText);
        } catch (err) {
            console.warn(`[WSS] _offerHint failed: ${err.message}`);
        } finally {
            state.novaProcessing = false;
        }
    }

    // ── Answer recording ──────────────────────────────────────────────────────

    /**
     * Handle a tap-answer result from Unity's question card.
     *
     * Two things happen:
     *   1. POST /answer on the Python agent — updates session stats in Redis
     *      AND returns Ms. Nova's verbal reaction (praise or LLM explanation).
     *   2. Synthesize TTS for that reaction and send nova_speaking back to Unity
     *      so the conversation continues naturally after every answer tap.
     *
     * Correct  → praise phrase  (hardcoded, low latency)
     * Wrong    → LLM explanation (agent references the actual question from history)
     */
    async _handleAnswerSubmit(state, msg) {
        const isCorrect = msg.isCorrect === true;
        console.log(`[WSS] answer_submit — session: ${state.sessionId}, correct: ${isCorrect}`);

        let responseText    = null;
        let animationEmotion = isCorrect ? 'Celebrating' : 'Concerned';

        try {
            const res = await fetch(`${AGENT_BASE_URL}/answer`, {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({
                    session_id: state.sessionId,
                    is_correct: isCorrect,
                }),
                // Wrong answers call the LLM — allow up to 15 s (same as /chat)
                signal: AbortSignal.timeout(15000),
            });

            if (res.ok) {
                const data   = await res.json();
                responseText     = data.response_text     ?? null;
                animationEmotion = data.animation_emotion ?? animationEmotion;
            } else {
                console.warn(`[WSS] /answer returned ${res.status} — using fallback phrase.`);
            }
        } catch (err) {
            console.warn(`[WSS] answer_submit agent call failed (non-fatal): ${err.message}`);
        }

        // Fallback phrases if agent is down or returned nothing
        if (!responseText) {
            responseText = isCorrect
                ? "Great job! That's correct — keep it up!"
                : "Not quite, but that's okay! Let's look at the correct answer together.";
        }

        // Play the animation hint before TTS starts (mirrors _callAgent pattern)
        if (animationEmotion && animationEmotion !== 'Talking') {
            this._send(state.ws, { type: 'animation_trigger', emotion: animationEmotion });
        }

        // Synthesize TTS and send nova_speaking — this is what makes Nova actually speak
        const durationMs = await this._speakAsNova(state, responseText);

        console.log(`[WSS] answer_submit response sent (correct: ${isCorrect}) — "${responseText.substring(0, 60)}…"`);

        // ── Auto-continue the lesson ──────────────────────────────────────────
        // Wait for the client to finish playing the praise/explanation audio,
        // then advance the curriculum and speak the next problem transition.
        // This keeps the session flowing without the child having to press PTT.
        const playbackBuffer = 900;   // ms padding for network + audio system startup
        const waitMs = Math.max(durationMs + playbackBuffer, 2000);
        console.log(`[WSS] Scheduling lesson continuation in ${waitMs}ms (session: ${state.sessionId})`);
        this._delay(waitMs).then(() => this._continueLesson(state, isCorrect));
    }

    /**
     * Called automatically after an MC answer response has been spoken.
     * Asks the Python agent to advance to the next problem and generates Nova's
     * transition text — no PTT required.
     */
    async _continueLesson(state, isCorrect) {
        if (state.ws.readyState !== WebSocket.OPEN) return;
        if (state.sessionEnding) return;

        console.log(`[WSS] _continueLesson — advancing curriculum (session: ${state.sessionId})`);

        let data = null;
        try {
            const res = await fetch(`${AGENT_BASE_URL}/session/continue`, {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({
                    session_id: state.sessionId,
                    is_correct: isCorrect,
                }),
                signal: AbortSignal.timeout(18000),
            });
            if (res.ok) {
                data = await res.json();
            } else {
                console.warn(`[WSS] /session/continue returned ${res.status}`);
            }
        } catch (err) {
            console.warn(`[WSS] /session/continue failed (non-fatal): ${err.message}`);
        }

        if (!data) return;

        // Drive animation before speech starts.
        if (data.animation_emotion && data.animation_emotion !== 'Talking') {
            this._send(state.ws, { type: 'animation_trigger', emotion: data.animation_emotion });
        }

        // HUD dot strip can update immediately — subtle UI, not problem content.
        if (data.chunk_phase) {
            this._send(state.ws, { type: 'chunk_phase', phase: data.chunk_phase });
        }

        // Claim generation token before the delay so any later turn (_offerHint,
        // _onTranscript) can invalidate this pending card send.
        const continueGen = ++state.cardGeneration;

        // Speak Nova's transition text FIRST.
        // Both the whiteboard card and the MC card are held until she finishes
        // so neither appears while she is still talking.
        let continuePlaybackMs = 0;
        if (data.response_text) {
            continuePlaybackMs = await this._speakAsNova(state, data.response_text);
        }

        // Wait for Unity to confirm playback is done before revealing visual cards.
        if (data.whiteboard_problem || data.question_display) {
            await this._waitForPlayback(state, continuePlaybackMs);
        }

        // Stale check — _offerHint or _onTranscript may have incremented cardGeneration
        // while we were awaiting, meaning a newer turn is now responsible for cards.
        if (state.cardGeneration !== continueGen) {
            console.log(`[WSS] continueLesson question_display skipped — stale gen ${continueGen} vs current ${state.cardGeneration}`);
            return;
        }

        if (data.whiteboard_problem) {
            console.log(`[WSS] continueLesson — whiteboard_update for new problem`);
            this._send(state.ws, {
                type:         'whiteboard_update',
                problem:      data.whiteboard_problem,
                steps:        data.whiteboard_steps ?? [],
                languageCode: state.languageCode,
            });
        }

        if (data.question_display) {
            console.log(`[WSS] continueLesson — question_display for new problem`);
            this._send(state.ws, {
                type:         'question_display',
                text:         data.question_display.text         ?? '',
                choices:      data.question_display.choices      ?? [],
                correctIndex: data.question_display.correct_index ?? 0,
            });
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
