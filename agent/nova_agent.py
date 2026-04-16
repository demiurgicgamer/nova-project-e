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

from curriculum_engine import CurriculumEngine, CurriculumState

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
_llm_client = None           # type varies by LLM_PROVIDER
_curriculum: Optional[CurriculumEngine] = None


@app.on_event("startup")
async def startup():
    global _redis_client, _llm_client, _curriculum

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

    # Initialise curriculum engine (connects to PostgreSQL)
    _curriculum = CurriculumEngine()
    await _curriculum.init()

    log.info("Nova Agent started.")


@app.on_event("shutdown")
async def shutdown():
    if _redis_client:
        await _redis_client.aclose()
    if _curriculum:
        await _curriculum.close()


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
    animation_emotion:  str         = "Talking"   # maps to TeacherAnimator EmotionState
    whiteboard_problem: Optional[str] = None       # set when a new problem is introduced
    whiteboard_steps:   list[str]   = []           # solution steps to reveal on whiteboard

class SessionStartRequest(BaseModel):
    session_id:    str
    child_profile: ChildProfile

class SessionEndRequest(BaseModel):
    session_id: str

class SessionIntroRequest(BaseModel):
    session_id:    str
    child_profile: ChildProfile


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
    session_id:         str
    child_profile:      dict
    emotion_state:      str
    user_message:       str
    history:            list    # list of {"role": "user"/"assistant", "content": str}
    pedagogy_hint:      str
    nova_response:      str
    animation:          str
    curriculum_state:   dict    # serialised CurriculumState — loaded pre-graph, saved post-graph
    whiteboard_problem: str     # non-empty when a new problem is introduced this turn
    whiteboard_steps:   list    # solution steps for the new problem (shown on whiteboard)


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


async def node_select_pedagogy(state: AgentState) -> AgentState:
    """
    Determine teaching strategy + load/refresh the active curriculum problem.

    Curriculum logic:
      1. Deserialise CurriculumState from state["curriculum_state"]
      2. If no problem is active, or the current problem is exhausted → fetch next problem
      3. Append the Socratic coaching context (problem + next hint) to pedagogy_hint
      4. Serialise updated CurriculumState back into state["curriculum_state"]

    The coaching context is injected into Ms. Nova's system prompt as an
    [Internal coaching] block — she uses it to guide the student without
    revealing the full solution.
    """
    history_len = len(state.get("history", []))
    cs_dict     = state.get("curriculum_state") or {}
    cs          = CurriculumState.from_dict(cs_dict) if cs_dict else CurriculumState()
    profile     = ChildProfile(**state["child_profile"])

    # ── Turn-count strategy hint ──────────────────────────────────────────────
    if history_len == 0:
        strategy = "Opening turn — if no topic is established yet, ask the student what they are working on."
    elif history_len < 4:
        strategy = "Early in session — confirm the topic, ask what the student already knows."
    else:
        strategy = "Mid-session — use Socratic questioning to guide. Refer to earlier answers where relevant."

    # ── Curriculum problem selection ──────────────────────────────────────────
    if _curriculum:
        # Fetch a new problem if: none is active, or the current one is exhausted
        if not cs.has_problem or cs.is_exhausted:
            if cs.has_problem and cs.is_exhausted:
                # Problem finished — update difficulty and count it
                cs.difficulty    = _curriculum.next_difficulty(
                    cs.session_correct, cs.session_total, cs.difficulty
                )
                cs.session_total += 1
                log.info(f"[{state['session_id']}] Problem exhausted — next difficulty: {cs.difficulty}")

            problem = await _curriculum.select_problem(
                topic_key   = cs.topic_key or _curriculum._fallback_topic_key(profile.grade),
                grade       = profile.grade,
                language    = profile.language,
                difficulty  = cs.difficulty,
                exclude_ids = cs.problems_seen,
            )
            if problem:
                cs.load_problem(problem)
                log.info(
                    f"[{state['session_id']}] New problem loaded: "
                    f"{problem.topic_key} / diff={problem.difficulty} / id={problem.id[:8]}"
                )

        coaching = CurriculumEngine.build_coaching_context(cs, profile.language)
    else:
        coaching = ""

    # ── Combine into pedagogy_hint ────────────────────────────────────────────
    base_hint = state.get("pedagogy_hint", "")
    parts     = [p for p in [base_hint, f"Strategy: {strategy}", coaching] if p]
    state["pedagogy_hint"]    = "\n\n".join(parts)
    state["curriculum_state"] = cs.to_dict()
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

    # ── Whiteboard: show problem when it is newly loaded this turn ────────────
    # is_new_problem is true only on the first turn after load_problem() is called
    # (hints_given == 0). The whiteboard displays the problem + its steps so the
    # student can see the full question while Nova speaks her first guiding question.
    cs_dict = state.get("curriculum_state") or {}
    if cs_dict:
        cs = CurriculumState.from_dict(cs_dict)
        if cs.is_new_problem:
            state["whiteboard_problem"] = cs.problem_text
            state["whiteboard_steps"]   = cs.solution_steps
            log.info(f"[{state['session_id']}] Whiteboard update — new problem shown")
        else:
            state["whiteboard_problem"] = ""
            state["whiteboard_steps"]   = []
    else:
        state["whiteboard_problem"] = ""
        state["whiteboard_steps"]   = []

    return state


async def node_update_context(state: AgentState) -> AgentState:
    """
    Append this turn to conversation history and persist history + curriculum state in Redis.
    Also increments hints_given so the next turn guides toward the next solution step.
    """
    # ── Conversation history ──────────────────────────────────────────────────
    history = list(state.get("history", []))
    history.append({"role": "user",      "content": state["user_message"]})
    history.append({"role": "assistant", "content": state["nova_response"]})

    # Keep last 20 messages (10 turns) to avoid prompt bloat
    if len(history) > 20:
        history = history[-20:]

    state["history"] = history

    # ── Curriculum state: advance hints pointer ───────────────────────────────
    cs_dict = state.get("curriculum_state") or {}
    if cs_dict:
        cs = CurriculumState.from_dict(cs_dict)
        if cs.has_problem:
            cs.hints_given += 1
            cs.turn_count  += 1
        state["curriculum_state"] = cs.to_dict()

    # ── Persist to Redis ──────────────────────────────────────────────────────
    if _redis_client:
        session_id = state["session_id"]
        try:
            hist_key = f"nova:session:{session_id}:history"
            curr_key = f"nova:session:{session_id}:curriculum"
            await _redis_client.set(hist_key, json.dumps(history),                   ex=SESSION_TTL_SEC)
            await _redis_client.set(curr_key, json.dumps(state["curriculum_state"]), ex=SESSION_TTL_SEC)
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


# ── Helpers: Redis load ───────────────────────────────────────────────────────

async def _load_history(session_id: str) -> list:
    if not _redis_client:
        return []
    key = f"nova:session:{session_id}:history"
    try:
        raw = await _redis_client.get(key)
        return json.loads(raw) if raw else []
    except Exception as e:
        log.warning(f"[{session_id}] Redis history read failed: {e}")
        return []


async def _load_curriculum_state(session_id: str) -> dict:
    if not _redis_client:
        return {}
    key = f"nova:session:{session_id}:curriculum"
    try:
        raw = await _redis_client.get(key)
        return json.loads(raw) if raw else {}
    except Exception as e:
        log.warning(f"[{session_id}] Redis curriculum read failed: {e}")
        return {}


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

    Also pre-selects the first topic and problem so the first turn of /chat
    has a curriculum context ready immediately.
    """
    profile = req.child_profile

    # ── Store child profile ───────────────────────────────────────────────────
    if _redis_client:
        try:
            await _redis_client.set(
                f"nova:session:{req.session_id}:profile",
                profile.model_dump_json(),
                ex=SESSION_TTL_SEC,
            )
        except Exception as e:
            log.warning(f"[{req.session_id}] Failed to store profile: {e}")

    # ── Pre-load first topic + problem ────────────────────────────────────────
    cs = CurriculumState()

    if _curriculum:
        try:
            topic = await _curriculum.select_topic(
                grade        = profile.grade,
                language     = profile.language,
                weak_topics  = profile.weak_topics,
                covered_today= [],
            )
            cs.topic_key  = topic["topic_key"]
            cs.topic_name = topic["topic_name"]

            problem = await _curriculum.select_problem(
                topic_key  = cs.topic_key,
                grade      = profile.grade,
                language   = profile.language,
                difficulty = cs.difficulty,
                exclude_ids= [],
            )
            if problem:
                cs.load_problem(problem)
                log.info(
                    f"[{req.session_id}] Pre-loaded problem: "
                    f"{problem.topic_key} / diff={problem.difficulty} / {problem.id[:8]}"
                )
        except Exception as e:
            log.warning(f"[{req.session_id}] Curriculum pre-load failed: {e}")

    if _redis_client:
        try:
            await _redis_client.set(
                f"nova:session:{req.session_id}:curriculum",
                json.dumps(cs.to_dict()),
                ex=SESSION_TTL_SEC,
            )
        except Exception as e:
            log.warning(f"[{req.session_id}] Failed to store curriculum state: {e}")

    log.info(
        f"[{req.session_id}] Session started — child: {profile.name}, "
        f"grade: {profile.grade}, lang: {profile.language}, "
        f"topic: {cs.topic_key or 'pending'}"
    )
    return {"status": "ok", "session_id": req.session_id}


@app.post("/session/intro", response_model=ChatResponse)
async def session_intro(req: SessionIntroRequest):
    """
    Generate Ms. Nova's opening welcome + push-to-talk guide.
    Called once by Node.js immediately after session_start is acknowledged.
    Stores the intro as the first assistant message in history so subsequent
    /chat calls have correct context (Nova spoke first).
    """
    profile = req.child_profile
    name    = profile.name
    lang    = profile.language

    if lang == "fr":
        intro_prompt = (
            f"Tu commences une nouvelle session de tutorat avec {name}. "
            f"Accueille-le/la chaleureusement par son prénom, présente-toi brièvement, "
            f"puis explique en une phrase comment fonctionne le bouton: "
            f"'Maintiens le bouton appuyé pour parler, relâche quand tu as terminé.' "
            f"Termine en lui demandant ce qu'on va travailler aujourd'hui. "
            f"2–3 phrases maximum. Sois chaleureuse et enthousiaste."
        )
    else:
        intro_prompt = (
            f"You are starting a new tutoring session with {name}. "
            f"Welcome them warmly by name, briefly introduce yourself as Ms. Nova, "
            f"then explain the push-to-talk button in one simple sentence: "
            f"'Hold the button to talk, release when you're done.' "
            f"Then invite them to tell you what they're working on today. "
            f"2–3 sentences maximum. Be warm and enthusiastic."
        )

    system = build_system_prompt(profile)

    if _llm_client is None:
        if lang == "fr":
            intro_text = (
                f"Bonjour {name}! Je suis Mme Nova, ta tutrice de maths. "
                f"Pour me parler, maintiens le bouton appuyé et relâche quand tu as terminé. "
                f"Alors, qu'est-ce qu'on travaille aujourd'hui?"
            )
        else:
            intro_text = (
                f"Hi {name}, I'm Ms. Nova — great to meet you! "
                f"To talk to me, just hold the button and release when you're done. "
                f"So, what are we working on today?"
            )
        log.warning(f"[{req.session_id}] No LLM configured — using hardcoded intro.")
    else:
        try:
            intro_text = await _call_llm(
                system,
                [{"role": "user", "content": intro_prompt}],
                req.session_id,
            )
        except Exception as e:
            log.error(f"[{req.session_id}] Intro LLM error: {e}")
            intro_text = (
                f"Hi {name}, I'm Ms. Nova — great to meet you! "
                f"Hold the button to talk, release when you're done. "
                f"What are we working on today?"
            )

    # Store intro as the first assistant message so /chat calls have correct context
    if _redis_client:
        history = [{"role": "assistant", "content": intro_text}]
        key = f"nova:session:{req.session_id}:history"
        try:
            await _redis_client.set(key, json.dumps(history), ex=SESSION_TTL_SEC)
        except Exception as e:
            log.warning(f"[{req.session_id}] Failed to store intro history: {e}")

    log.info(f"[{req.session_id}] Intro generated for {name}: \"{intro_text[:80]}\"")
    return ChatResponse(response_text=intro_text, animation_emotion="Talking")


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
            f"nova:session:{req.session_id}:curriculum",
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

    Called by Node.js SessionWebSocketServer._onTranscript().
    Returns response_text, animation_emotion, and (when a new problem is introduced)
    whiteboard_problem + whiteboard_steps for display in the Unity whiteboard.
    """
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")

    # Load conversation history + curriculum state from Redis
    history          = await _load_history(req.session_id)
    curriculum_state = await _load_curriculum_state(req.session_id)

    # Build initial state for the graph
    initial_state: AgentState = {
        "session_id":         req.session_id,
        "child_profile":      req.child_profile.model_dump(),
        "emotion_state":      req.emotion_state,
        "user_message":       req.text,
        "history":            history,
        "pedagogy_hint":      "",
        "nova_response":      "",
        "animation":          "Talking",
        "curriculum_state":   curriculum_state,
        "whiteboard_problem": "",
        "whiteboard_steps":   [],
    }

    # Run the LangGraph agent
    try:
        final_state = await _agent_graph.ainvoke(initial_state)
    except Exception as e:
        log.error(f"[{req.session_id}] Graph execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Agent processing failed")

    return ChatResponse(
        response_text      = final_state["nova_response"],
        animation_emotion  = final_state.get("animation", "Talking"),
        whiteboard_problem = final_state.get("whiteboard_problem") or None,
        whiteboard_steps   = final_state.get("whiteboard_steps") or [],
    )
