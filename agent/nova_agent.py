"""
nova_agent.py — Ms. Nova AI Tutor Agent
Day 28: LangGraph + Claude API

FastAPI service exposing a /chat endpoint.
The Node.js SessionWebSocketServer POSTs child transcripts here
and receives Ms. Nova's response text + animation hint.

Architecture:
  StateGraph nodes:
    receive_message  → load session context from Redis
    check_emotion    → adjust tone based on child's emotional state
    select_pedagogy  → pick teaching strategy for this turn
    generate_response → call Claude API as Ms. Nova
    update_context   → persist updated session state to Redis

HTTP endpoints:
  POST /chat        — single turn: child message → Nova response
  POST /session/start  — initialise session context in Redis
  POST /session/end    — flush summary, clean up Redis
  GET  /health      — liveness check
"""

import json
import os
import logging
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from langgraph.graph import StateGraph, END
from dotenv import load_dotenv

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nova_agent")

# ── Config ────────────────────────────────────────────────────────────────────
# Set LLM_PROVIDER in .env to switch AI backend:
#   "claude"  — Anthropic Claude (production quality, paid)
#   "gemini"  — Google Gemini 1.5 Flash (free tier: 1500 req/day)
#   "groq"    — Groq cloud (free tier: fast llama3/mixtral)
#   "ollama"  — Local Ollama (completely free, offline)
LLM_PROVIDER    = os.getenv("LLM_PROVIDER", "claude").lower()

CLAUDE_API_KEY  = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_MODEL    = "claude-opus-4-5"

GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL    = "gemini-2.0-flash-lite"

GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL      = "llama-3.3-70b-versatile"   # best free Groq model for instruction following

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")

REDIS_URL       = os.getenv("REDIS_URL", "redis://redis:6379")
REDIS_PASSWORD  = os.getenv("REDIS_PASSWORD", "")
SESSION_TTL_SEC = 7200   # 2 hours — max session lifetime in Redis

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Nova Agent", version="1.0.0")

# ── Clients (initialised at startup) ─────────────────────────────────────────
_redis_client: Optional[aioredis.Redis] = None
_llm_client = None   # type varies by LLM_PROVIDER


@app.on_event("startup")
async def startup():
    global _redis_client, _llm_client

    _redis_client = aioredis.from_url(
        REDIS_URL,
        password=REDIS_PASSWORD or None,
        decode_responses=True,
    )

    if LLM_PROVIDER == "claude":
        if not CLAUDE_API_KEY or CLAUDE_API_KEY == "placeholder":
            log.warning("CLAUDE_API_KEY not set — will use fallback responses.")
            _llm_client = None
        else:
            import anthropic
            _llm_client = anthropic.AsyncAnthropic(api_key=CLAUDE_API_KEY)
            log.info(f"LLM: Anthropic Claude ({CLAUDE_MODEL})")

    elif LLM_PROVIDER == "gemini":
        if not GEMINI_API_KEY or GEMINI_API_KEY == "placeholder":
            log.warning("GEMINI_API_KEY not set — will use fallback responses.")
            _llm_client = None
        else:
            from google import genai
            # Use v1 API — required for gemini-1.5-flash (v1beta is for gemini-2.x only)
            _llm_client = genai.Client(api_key=GEMINI_API_KEY)
            log.info(f"LLM: Google Gemini ({GEMINI_MODEL}) — free tier")

    elif LLM_PROVIDER == "groq":
        if not GROQ_API_KEY or GROQ_API_KEY == "placeholder":
            log.warning("GROQ_API_KEY not set — will use fallback responses.")
            _llm_client = None
        else:
            from groq import AsyncGroq
            _llm_client = AsyncGroq(api_key=GROQ_API_KEY)
            log.info(f"LLM: Groq ({GROQ_MODEL}) — free tier")

    elif LLM_PROVIDER == "ollama":
        import httpx
        _llm_client = httpx.AsyncClient(base_url=OLLAMA_BASE_URL, timeout=60.0)
        log.info(f"LLM: Ollama local ({OLLAMA_MODEL}) @ {OLLAMA_BASE_URL}")

    else:
        log.warning(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}' — will use fallback responses.")
        _llm_client = None

    log.info("Nova Agent started.")


@app.on_event("shutdown")
async def shutdown():
    if _redis_client:
        await _redis_client.aclose()


# ── Pydantic models ───────────────────────────────────────────────────────────

class ChildProfile(BaseModel):
    child_id:   str
    grade:      int   = 6
    name:       str   = "friend"
    language:   str   = "en"   # "en" | "fr"
    weak_topics: list[str] = []

class ChatRequest(BaseModel):
    session_id:    str
    text:          str
    child_profile: ChildProfile
    emotion_state: str = "NEUTRAL"   # NEUTRAL | CONFUSED | FRUSTRATED | BORED | ENGAGED | CONFIDENT

class ChatResponse(BaseModel):
    response_text:      str
    animation_emotion:  str = "Talking"   # maps to TeacherAnimator EmotionState
    whiteboard_problem: Optional[str] = None
    whiteboard_steps:   list[str] = []

class SessionStartRequest(BaseModel):
    session_id:    str
    child_profile: ChildProfile

class SessionEndRequest(BaseModel):
    session_id: str


# ── System prompt builder ─────────────────────────────────────────────────────

def build_system_prompt(child_profile: ChildProfile) -> str:
    """
    Build Ms. Nova's system prompt for this child's session.
    Language-specific variants for EN and FR (Canadian).
    """
    name    = child_profile.name
    grade   = child_profile.grade
    lang    = child_profile.language
    weak    = child_profile.weak_topics

    weak_str = ", ".join(weak) if weak else "none identified yet"

    if lang == "fr":
        return f"""Tu es Mme Nova, une tutrice de mathématiques chaleureuse et professionnelle pour des élèves de {grade}e année au Canada.

Personnalité:
- Patiente, encourageante et positive
- Tu utilises un langage simple et adapté à l'âge de l'élève
- Tu poses des questions guidantes plutôt que de donner des réponses directes
- Tu félicites les efforts, pas seulement les bonnes réponses
- Tu restes toujours dans le sujet des mathématiques

L'élève s'appelle {name}. Ses points faibles actuels: {weak_str}.

Règles pédagogiques strictes:
1. Ne jamais donner la réponse directement — guide l'élève par des questions
2. Si l'élève est bloqué, décompose le problème en étapes plus petites
3. Utilise des exemples de la vie quotidienne canadienne (hockey, Tim Hortons, températures en Celsius)
4. Limite tes réponses à 2-3 phrases maximum pour maintenir l'engagement
5. Termine chaque réponse par une question qui fait réfléchir l'élève
6. Si l'élève semble frustré, valide ses émotions et simplifie encore plus

Tu réponds uniquement en français canadien. Sois concise et chaleureuse."""

    else:  # default: English Canadian
        return f"""You are Ms. Nova, a warm and professional math tutor for Grade {grade} students in Canada.

Personality:
- Patient, encouraging, and positive at all times
- You use age-appropriate language — clear, never condescending
- You guide with questions rather than giving direct answers
- You celebrate effort and persistence, not just correct answers
- You stay strictly on-topic (mathematics)

Your student is named {name}. Their current weak areas: {weak_str}.

Strict pedagogical rules:
1. NEVER give the answer directly — guide the student with leading questions
2. If a student is stuck, break the problem into smaller steps
3. Use relatable Canadian examples (hockey stats for ratios, Tim Hortons for percentages, temperature in Celsius)
4. Keep responses to 2–3 sentences maximum to maintain engagement
5. End every response with a question that makes the student think
6. If the student seems frustrated, validate their feelings and simplify further

Respond only in Canadian English. Be warm, concise, and encouraging."""


# ── LangGraph state & nodes ───────────────────────────────────────────────────

class AgentState(dict):
    """Typed dictionary passed between LangGraph nodes."""
    session_id:       str
    child_profile:    dict
    emotion_state:    str
    user_message:     str
    history:          list    # list of {"role": "user"/"assistant", "content": str}
    pedagogy_hint:    str
    nova_response:    str
    animation:        str


def node_receive_message(state: AgentState) -> AgentState:
    """Load conversation history from Redis cache (sync stub — actual load happens in endpoint)."""
    # History is pre-loaded by the endpoint before calling the graph
    log.info(f"[{state['session_id']}] receive_message — emotion: {state['emotion_state']}, "
             f"msg: \"{state['user_message'][:60]}\"")
    return state


def node_check_emotion(state: AgentState) -> AgentState:
    """Map child emotion → pedagogy adjustment hint."""
    emotion = state.get("emotion_state", "NEUTRAL")

    hints = {
        "CONFUSED":    "The student seems confused. Slow down significantly. Ask what specific part is unclear. Break into the smallest possible step.",
        "FRUSTRATED":  "The student is frustrated. First validate their feelings ('I know this part is tricky!'). Offer an easier related problem to rebuild confidence before returning to the hard one.",
        "BORED":       "The student seems disengaged. Introduce a surprising real-world connection or a mini challenge to spark curiosity.",
        "ENGAGED":     "The student is engaged and focused. Maintain pace. Slightly increase complexity if they answer correctly.",
        "CONFIDENT":   "The student is confident. Great time to gently push to the next level of difficulty or introduce a connected concept.",
        "NEUTRAL":     "Neutral state. Use standard Socratic approach — guide with questions, affirm effort.",
    }

    state["pedagogy_hint"] = hints.get(emotion, hints["NEUTRAL"])
    log.info(f"[{state['session_id']}] pedagogy_hint set for emotion: {emotion}")
    return state


def node_select_pedagogy(state: AgentState) -> AgentState:
    """
    Determine which teaching strategy to use this turn.
    In future versions this will consult CurriculumEngine to pick the
    right topic and difficulty — for MVP it enriches the system prompt hint.
    """
    history_len = len(state.get("history", []))

    if history_len == 0:
        strategy = "Opening turn: greet the student warmly, ask what topic they are working on today."
    elif history_len < 4:
        strategy = "Early in session: establish what the student already knows about this topic."
    else:
        strategy = "Mid-session: guide toward solution using Socratic questioning. Refer to earlier answers if relevant."

    # Combine pedagogy hint + strategy into a single instruction
    state["pedagogy_hint"] = state.get("pedagogy_hint", "") + f"\n\nStrategy: {strategy}"
    return state


async def node_generate_response(state: AgentState) -> AgentState:
    """Call Claude API as Ms. Nova and produce a response."""
    profile = ChildProfile(**state["child_profile"])
    system  = build_system_prompt(profile)

    # Append the pedagogy hint as a final system instruction
    pedagogy = state.get("pedagogy_hint", "")
    if pedagogy:
        system += f"\n\n[Internal coaching for this turn — do NOT mention to student]\n{pedagogy}"

    # Build messages array from history + current user message
    messages = list(state.get("history", []))
    messages.append({"role": "user", "content": state["user_message"]})

    # LLM call — routed to whichever provider is configured
    if _llm_client is None:
        response_text = (
            f"That's a great question, {profile.name}! "
            f"Let me think about that with you. What do you already know about this topic?"
        )
        log.warning(f"[{state['session_id']}] No LLM configured — using fallback response.")
    else:
        try:
            response_text = await _call_llm(system, messages, state['session_id'])
            log.info(f"[{state['session_id']}] LLM response ({len(response_text)} chars): \"{response_text[:80]}\"")
        except Exception as e:
            log.error(f"[{state['session_id']}] LLM error: {e}")
            response_text = (
                "I'm having a little trouble right now. "
                "Can you tell me the problem again so I can help you better?"
            )

    state["nova_response"] = response_text

    # Map emotion → animation emotion for TeacherAnimator
    emotion_to_animation = {
        "FRUSTRATED": "Encouraging",
        "CONFUSED":   "Concerned",
        "BORED":      "Talking",
        "ENGAGED":    "Talking",
        "CONFIDENT":  "Celebrating",
        "NEUTRAL":    "Talking",
    }
    state["animation"] = emotion_to_animation.get(state.get("emotion_state", "NEUTRAL"), "Talking")

    return state


async def node_update_context(state: AgentState) -> AgentState:
    """Append this turn to conversation history and persist in Redis."""
    history = list(state.get("history", []))
    history.append({"role": "user",      "content": state["user_message"]})
    history.append({"role": "assistant", "content": state["nova_response"]})

    # Keep last 20 messages (10 turns) to avoid prompt bloat
    if len(history) > 20:
        history = history[-20:]

    state["history"] = history

    # Persist to Redis
    if _redis_client:
        key = f"nova:session:{state['session_id']}:history"
        try:
            await _redis_client.set(key, json.dumps(history), ex=SESSION_TTL_SEC)
        except Exception as e:
            log.warning(f"[{state['session_id']}] Redis write failed: {e}")

    return state


# ── Build the LangGraph ───────────────────────────────────────────────────────

async def _call_llm(system: str, messages: list, session_id: str) -> str:
    """Route LLM call to the configured provider."""

    if LLM_PROVIDER == "claude":
        import anthropic
        message = await _llm_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            system=system,
            messages=messages,
        )
        return message.content[0].text.strip()

    elif LLM_PROVIDER == "gemini":
        from google.genai import types as genai_types
        # Combine system + history into a single prompt string
        history_text = "\n".join(
            f"{'Student' if m['role'] == 'user' else 'Ms. Nova'}: {m['content']}"
            for m in messages[:-1]
        )
        latest = messages[-1]["content"]
        prompt = f"{system}\n\n{history_text}\n\nStudent: {latest}\nMs. Nova:"
        response = await _llm_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=256,
                temperature=0.7,
            ),
        )
        return response.text.strip()

    elif LLM_PROVIDER == "groq":
        chat_messages = [{"role": "system", "content": system}] + messages
        response = await _llm_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=chat_messages,
            max_tokens=256,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()

    elif LLM_PROVIDER == "ollama":
        chat_messages = [{"role": "system", "content": system}] + messages
        resp = await _llm_client.post(
            "/api/chat",
            json={"model": OLLAMA_MODEL, "messages": chat_messages, "stream": False,
                  "options": {"num_predict": 256}},
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()

    raise RuntimeError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER}")


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("receive_message",   node_receive_message)
    graph.add_node("check_emotion",     node_check_emotion)
    graph.add_node("select_pedagogy",   node_select_pedagogy)
    graph.add_node("generate_response", node_generate_response)
    graph.add_node("update_context",    node_update_context)

    graph.set_entry_point("receive_message")
    graph.add_edge("receive_message",   "check_emotion")
    graph.add_edge("check_emotion",     "select_pedagogy")
    graph.add_edge("select_pedagogy",   "generate_response")
    graph.add_edge("generate_response", "update_context")
    graph.add_edge("update_context",    END)

    return graph.compile()


_agent_graph = build_graph()


# ── Helper: load history from Redis ──────────────────────────────────────────

async def _load_history(session_id: str) -> list:
    if not _redis_client:
        return []
    key = f"nova:session:{session_id}:history"
    try:
        raw = await _redis_client.get(key)
        return json.loads(raw) if raw else []
    except Exception as e:
        log.warning(f"[{session_id}] Redis read failed: {e}")
        return []


# ── HTTP Endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    redis_ok = False
    if _redis_client:
        try:
            await _redis_client.ping()
            redis_ok = True
        except Exception:
            pass

    return {
        "status":      "ok",
        "llm_provider":  LLM_PROVIDER,
        "llm_ready":     _llm_client is not None,
        "redis_ready":  redis_ok,
    }


@app.post("/session/start")
async def session_start(req: SessionStartRequest):
    """
    Initialise session context in Redis.
    Called by Node.js SessionWebSocketServer when session_start event arrives.
    """
    key = f"nova:session:{req.session_id}:profile"
    if _redis_client:
        try:
            await _redis_client.set(
                key,
                req.child_profile.model_dump_json(),
                ex=SESSION_TTL_SEC,
            )
        except Exception as e:
            log.warning(f"[{req.session_id}] Failed to store profile: {e}")

    log.info(f"[{req.session_id}] Session started — child: {req.child_profile.name}, "
             f"grade: {req.child_profile.grade}, lang: {req.child_profile.language}")

    return {"status": "ok", "session_id": req.session_id}


@app.post("/session/end")
async def session_end(req: SessionEndRequest):
    """
    Clean up Redis keys for the session.
    Called by Node.js when session_end event arrives.
    """
    if _redis_client:
        keys = [
            f"nova:session:{req.session_id}:history",
            f"nova:session:{req.session_id}:profile",
        ]
        try:
            await _redis_client.delete(*keys)
        except Exception as e:
            log.warning(f"[{req.session_id}] Redis cleanup failed: {e}")

    log.info(f"[{req.session_id}] Session ended — Redis keys cleaned up.")
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    Main endpoint: child's transcript → Ms. Nova's response.

    Called by Node.js SessionWebSocketServer._onTranscript()
    """
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")

    # Load conversation history from Redis
    history = await _load_history(req.session_id)

    # Build initial state for the graph
    initial_state: AgentState = {
        "session_id":    req.session_id,
        "child_profile": req.child_profile.model_dump(),
        "emotion_state": req.emotion_state,
        "user_message":  req.text,
        "history":       history,
        "pedagogy_hint": "",
        "nova_response": "",
        "animation":     "Talking",
    }

    # Run the LangGraph agent
    try:
        final_state = await _agent_graph.ainvoke(initial_state)
    except Exception as e:
        log.error(f"[{req.session_id}] Graph execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Agent processing failed")

    return ChatResponse(
        response_text=final_state["nova_response"],
        animation_emotion=final_state.get("animation", "Talking"),
    )
