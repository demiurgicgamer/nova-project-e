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
import random
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
SESSION_TTL_SEC      = 7200    # 2 hours  — active session Redis keys
CHECKPOINT_TTL_SEC   = 2592000 # 30 days  — per-child per-topic resume checkpoint

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
    animation_emotion:  str             = "Talking"   # maps to TeacherAnimator EmotionState
    whiteboard_problem: Optional[str]   = None        # set when a new problem is introduced
    whiteboard_steps:   list[str]       = []          # solution steps to reveal on whiteboard
    question_display:   Optional[dict]  = None        # {text, choices, correct_index} for question card
    chunk_phase:        str             = "intro"     # drives 4-dot HUD strip: intro|chunk_a|chunk_b|consolidate


class AnswerRequest(BaseModel):
    session_id: str
    is_correct: bool

class SessionContinueRequest(BaseModel):
    session_id: str
    is_correct: bool = True   # tone hint: correct → celebratory advance, wrong → empathetic advance

class SessionStartRequest(BaseModel):
    session_id:     str
    child_profile:  ChildProfile
    selected_topic: Optional[str] = None   # topic_key chosen by child in TopicPicker

class SessionEndRequest(BaseModel):
    session_id: str

class SessionIntroRequest(BaseModel):
    session_id:     str
    child_profile:  ChildProfile
    selected_topic: Optional[str] = None   # passed through so intro can name the topic


# ── System prompt builder ─────────────────────────────────────────────────────

def build_system_prompt(child_profile: ChildProfile, current_topic_name: Optional[str] = None) -> str:
    """
    Build Ms. Nova's system prompt for this child's session.
    Language-specific variants for EN and FR (Canadian).
    current_topic_name — human-readable topic the child selected (e.g. "Fractions").
    When provided, Nova is told to teach that topic and NOT ask what to work on.
    """
    name    = child_profile.name
    grade   = child_profile.grade
    lang    = child_profile.language
    weak    = child_profile.weak_topics

    weak_str = ", ".join(weak) if weak else "none identified yet"

    if current_topic_name:
        topic_line_fr = (
            f"\nSujet de la séance d'aujourd'hui: **{current_topic_name}**. "
            f"Commence à enseigner ce sujet immédiatement — ne demande PAS à l'élève ce qu'il veut apprendre."
        )
        topic_line_en = (
            f"\nToday's session topic: **{current_topic_name}**. "
            f"Begin teaching this topic immediately — do NOT ask the student what they want to work on."
        )
    else:
        topic_line_fr = ""
        topic_line_en = ""

    if lang == "fr":
        return f"""Tu es Mme Nova, une tutrice de mathématiques chaleureuse et professionnelle pour des élèves de {grade}e année au Canada.

Personnalité:
- Patiente, encourageante et positive
- Tu utilises un langage simple et adapté à l'âge de l'élève
- Tu poses des questions guidantes plutôt que de donner des réponses directes
- Tu félicites les efforts, pas seulement les bonnes réponses
- Tu restes toujours dans le sujet des mathématiques

L'élève s'appelle {name}. Ses points faibles actuels: {weak_str}.{topic_line_fr}

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

Your student is named {name}. Their current weak areas: {weak_str}.{topic_line_en}

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
    question_display:   dict    # {text, choices, correct_index} — None when no new question
    chunk_phase:        str     # current pedagogical phase from CurriculumState.chunk_phase


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
                # Problem finished — update difficulty.
                # mc_answered: /answer already incremented session_total → don't double-count.
                # Hint path: session_total counts problem completions, not individual answer taps.
                cs.difficulty = _curriculum.next_difficulty(
                    cs.session_correct, cs.session_total, cs.difficulty
                )
                if not cs.mc_answered:
                    cs.session_total += 1
                cs.mc_answered = False   # reset flag for the next problem
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

    # ── Whiteboard + question card: emitted on first turn of a new problem ────
    # is_new_problem is true only on the first turn after load_problem() is called
    # (hints_given == 0). The whiteboard shows problem text + steps; the question
    # card shows shuffled MC choices for tap-to-answer evaluation.
    cs_dict = state.get("curriculum_state") or {}
    if cs_dict:
        cs = CurriculumState.from_dict(cs_dict)
        if cs.is_new_problem:
            state["whiteboard_problem"] = cs.problem_text
            state["whiteboard_steps"]   = cs.solution_steps
            state["question_display"]   = CurriculumEngine.build_question_data(cs) if _curriculum else None
            log.info(
                f"[{state['session_id']}] New problem shown — "
                f"question_card={'yes' if state['question_display'] else 'no (no MC data)'}"
            )
        else:
            state["whiteboard_problem"] = ""
            state["whiteboard_steps"]   = []
            state["question_display"]   = None
        state["chunk_phase"] = cs.chunk_phase
    else:
        state["whiteboard_problem"] = ""
        state["whiteboard_steps"]   = []
        state["question_display"]   = None
        state["chunk_phase"]        = "intro"

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


async def _load_checkpoint(child_id: str, topic_key: str) -> dict:
    """
    Load a topic-level resume checkpoint for a child.
    This key survives session cleanup (30-day TTL) so the child picks up where
    they left off when they return to the same topic in a later session.

    Returns a dict with keys: problems_seen, difficulty.
    Returns {} if no checkpoint exists (first time on this topic).
    """
    if not _redis_client or not child_id or not topic_key:
        return {}
    key = f"nova:child:{child_id}:topic:{topic_key}:checkpoint"
    try:
        raw = await _redis_client.get(key)
        if raw:
            data = json.loads(raw)
            log.info(f"[checkpoint] Restored for child={child_id} topic={topic_key}: "
                     f"{len(data.get('problems_seen', []))} problems seen, diff={data.get('difficulty', 2)}")
            return data
    except Exception as e:
        log.warning(f"[checkpoint] Load failed for {child_id}/{topic_key}: {e}")
    return {}


async def _save_checkpoint(child_id: str, topic_key: str, cs: "CurriculumState") -> None:
    """
    Persist a topic-level resume checkpoint so the child continues from here next session.
    Called at session_end — stored for 30 days.
    """
    if not _redis_client or not child_id or not topic_key:
        return
    key = f"nova:child:{child_id}:topic:{topic_key}:checkpoint"
    payload = {
        "problems_seen": cs.problems_seen,
        "difficulty":    cs.difficulty,
    }
    try:
        await _redis_client.set(key, json.dumps(payload), ex=CHECKPOINT_TTL_SEC)
        log.info(f"[checkpoint] Saved for child={child_id} topic={topic_key}: "
                 f"{len(cs.problems_seen)} problems seen, diff={cs.difficulty}")
    except Exception as e:
        log.warning(f"[checkpoint] Save failed for {child_id}/{topic_key}: {e}")


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

    # ── Pre-load first topic + problem (with resume checkpoint) ──────────────
    cs = CurriculumState()

    if _curriculum:
        try:
            # If the child already chose a topic in the TopicPicker, use it directly.
            # Otherwise fall back to the priority-selection algorithm (weak areas first).
            if req.selected_topic:
                topic = await _curriculum.get_topic_by_key(
                    topic_key = req.selected_topic,
                    grade     = profile.grade,
                    language  = profile.language,
                )
                log.info(f"[{req.session_id}] Using child-selected topic: {topic['topic_key']} ({topic['topic_name']})")
            else:
                topic = await _curriculum.select_topic(
                    grade        = profile.grade,
                    language     = profile.language,
                    weak_topics  = profile.weak_topics,
                    covered_today= [],
                )

            cs.topic_key  = topic["topic_key"]
            cs.topic_name = topic["topic_name"]

            # ── Resume detection ──────────────────────────────────────────────
            # Priority 1: Redis checkpoint (set by _save_checkpoint at session_end).
            #   Contains problems_seen + difficulty from the previous session.
            # Priority 2: DB mastery bootstrap (catches existing users who have
            #   mastery recorded in child_topic_progress before the checkpoint
            #   feature was introduced — mastery_level > 0 OR attempt_count > 0).
            # is_resuming is stored in CurriculumState (Redis) so session_intro
            # reads exactly the same flag without making its own independent query.

            checkpoint = await _load_checkpoint(profile.child_id, cs.topic_key)
            if checkpoint and checkpoint.get("problems_seen"):
                cs.problems_seen = checkpoint.get("problems_seen", [])
                cs.difficulty    = checkpoint.get("difficulty", cs.difficulty)
                cs.is_resuming   = True
                log.info(
                    f"[{req.session_id}] Resuming from Redis checkpoint — "
                    f"{len(cs.problems_seen)} problems already seen, diff={cs.difficulty}"
                )
            else:
                # No checkpoint: fall back to DB mastery to detect returning users
                db_progress = {}
                if _curriculum:
                    db_progress = await _curriculum.get_child_topic_progress(
                        profile.child_id, cs.topic_key
                    )
                if db_progress.get("mastery_level", 0) > 0 or db_progress.get("attempt_count", 0) > 0:
                    cs.is_resuming = True
                    log.info(
                        f"[{req.session_id}] Resuming from DB mastery bootstrap — "
                        f"mastery={db_progress.get('mastery_level')}%, "
                        f"attempts={db_progress.get('attempt_count')}"
                    )
                else:
                    cs.is_resuming = False
                    log.info(f"[{req.session_id}] Fresh start — no prior history for topic: {cs.topic_key}")

            problem = await _curriculum.select_problem(
                topic_key  = cs.topic_key,
                grade      = profile.grade,
                language   = profile.language,
                difficulty = cs.difficulty,
                exclude_ids= cs.problems_seen,  # skip already-seen problems
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

    # Store the resolved topic key as a dedicated Redis key so session_intro
    # can reliably retrieve it without depending on how it was forwarded.
    # Priority: child-selected topic → curriculum-preloaded topic
    persisted_topic = req.selected_topic or cs.topic_key or ""
    if _redis_client and persisted_topic:
        try:
            await _redis_client.set(
                f"nova:session:{req.session_id}:topic",
                persisted_topic,
                ex=SESSION_TTL_SEC,
            )
        except Exception as e:
            log.warning(f"[{req.session_id}] Failed to store topic key: {e}")

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

    Topic resolution order (most → least reliable):
      1. req.selected_topic   — forwarded by WSS from session_start message
      2. nova:session:{id}:topic  — Redis key written by session_start
      3. nova:session:{id}:curriculum → topic_name field

    When the topic is known the intro is HARDCODED (fixed greeting + topic
    announcement + opening question). The LLM is NOT called for the topic-
    aware path because Gemini concatenates the system prompt as plain text
    and can hallucinate "what topic?" regardless of instructions.
    Only the no-topic fallback path uses the LLM.
    """
    profile = req.child_profile
    name    = profile.name
    lang    = profile.language

    # ── Step 1: Resolve topic key from all available sources ─────────────────
    topic_key: Optional[str] = None

    # Source A: forwarded by WSS
    if req.selected_topic:
        topic_key = req.selected_topic
        log.info(f"[{req.session_id}] Topic from request: {topic_key}")

    # Source B: dedicated Redis key written by session_start
    if not topic_key and _redis_client:
        try:
            stored = await _redis_client.get(f"nova:session:{req.session_id}:topic")
            if stored:
                topic_key = stored
                log.info(f"[{req.session_id}] Topic from Redis key: {topic_key}")
        except Exception:
            pass

    # Source C: curriculum state topic_key (may differ from child's choice only
    # when selected_topic wasn't forwarded and select_topic() picked one)
    if not topic_key and _redis_client:
        try:
            raw = await _redis_client.get(f"nova:session:{req.session_id}:curriculum")
            if raw:
                cs_dict   = json.loads(raw)
                topic_key = cs_dict.get("topic_key") or None
                log.info(f"[{req.session_id}] Topic from curriculum state: {topic_key}")
        except Exception:
            pass

    # ── Step 2: Resolve human-readable display name from topic key ────────────
    topic_name: Optional[str] = None
    if topic_key:
        # Try DB lookup first; always fall back to title-casing the key
        if _curriculum:
            try:
                t = await _curriculum.get_topic_by_key(topic_key, profile.grade, profile.language)
                topic_name = t.get("topic_name") or topic_key.replace("_", " ").title()
            except Exception:
                topic_name = topic_key.replace("_", " ").title()
        else:
            topic_name = topic_key.replace("_", " ").title()

    log.info(f"[{req.session_id}] Intro topic resolved → key={topic_key!r} name={topic_name!r}")

    # ── Step 3: Build intro text ──────────────────────────────────────────────
    #
    # When topic_name IS known:
    #   Use a hardcoded template — guaranteed to announce the topic correctly.
    #   No LLM call = no hallucination risk, lower latency for the first message.
    #
    # When topic_name is NOT known (edge case — no topic picked yet):
    #   Fall through to LLM-generated open-ended greeting.
    #
    if topic_name:
        # Read CurriculumState written by session_start.
        # session_start is the single source of truth — it checks BOTH the Redis
        # checkpoint (problems_seen) AND the DB mastery (child_topic_progress).
        is_resuming      = False
        resume_problem:  Optional[dict] = None   # pre-loaded problem to show immediately on resume
        resume_wb_steps: list[str]      = []
        resume_qd:       Optional[dict] = None   # question_display dict for question card
        resume_chunk:    str            = "intro"

        if _redis_client:
            try:
                raw_cs = await _redis_client.get(f"nova:session:{req.session_id}:curriculum")
                if raw_cs:
                    cs_dict      = json.loads(raw_cs)
                    is_resuming  = bool(cs_dict.get("is_resuming", False))

                    # When resuming, load the pre-selected problem so the intro
                    # can present it directly — child jumps straight into work,
                    # no "what do you remember" warmup needed.
                    if is_resuming:
                        cs_obj = CurriculumState.from_dict(cs_dict)
                        # chunk_phase is a @property — compute from the object, not cs_dict
                        resume_chunk = cs_obj.chunk_phase
                        if cs_obj.has_problem:
                            resume_problem  = {
                                "text":  cs_obj.problem_text,
                                "steps": cs_obj.solution_steps,
                            }
                            resume_wb_steps = cs_obj.solution_steps
                            if _curriculum and cs_obj.question_choices:
                                resume_qd = _curriculum.build_question_data(cs_obj)
            except Exception:
                pass

        # Hardcoded intro — topic always announced, never asks "what topic?"
        # Two variants: fresh start vs. resuming a previous session.
        #
        # Resume variant: skips the "what do you know" warmup — presents the
        # next problem directly so the child continues real work immediately.
        if lang == "fr":
            if is_resuming:
                if resume_problem:
                    intro_text = (
                        f"Bon retour, {name}! Contente de te revoir. "
                        f"On reprend {topic_name} là où on s'est arrêtés — voici ton prochain problème. "
                        f"Maintiens le bouton pour me répondre."
                    )
                else:
                    intro_text = (
                        f"Bon retour, {name}! Contente de te revoir. "
                        f"On continue {topic_name} — tu as déjà bien progressé! "
                        f"Maintiens le bouton pour me parler."
                    )
            else:
                intro_text = (
                    f"Bonjour {name}! Je suis Mme Nova, ta tutrice de maths. "
                    f"Pour me parler, maintiens le bouton appuyé et relâche quand tu as terminé. "
                    f"Aujourd'hui on travaille sur {topic_name} — super choix! "
                    f"Pour commencer, dis-moi ce que tu sais déjà sur {topic_name}."
                )
        else:
            if is_resuming:
                if resume_problem:
                    intro_text = (
                        f"Welcome back, {name}! Great to see you again. "
                        f"We're picking up {topic_name} right where we left off — here's your next problem. "
                        f"Hold the button to answer when you're ready."
                    )
                else:
                    intro_text = (
                        f"Welcome back, {name}! Great to see you again. "
                        f"We're continuing {topic_name} — you've already made great progress! "
                        f"Hold the button to talk when you're ready."
                    )
            else:
                intro_text = (
                    f"Hi {name}, I'm Ms. Nova — great to see you! "
                    f"Hold the button to talk, release when you're done. "
                    f"Today we're working on {topic_name} — excellent choice! "
                    f"To kick things off, tell me what you already know about {topic_name}."
                )
        log.info(
            f"[{req.session_id}] Hardcoded intro — topic: {topic_name}, "
            f"resuming: {is_resuming}, problem_attached: {resume_problem is not None}"
        )

    else:
        # No topic known — use LLM to generate open-ended welcome
        system = build_system_prompt(profile)

        if lang == "fr":
            intro_prompt = (
                f"Tu commences une nouvelle session de tutorat avec {name}. "
                f"Accueille-le/la chaleureusement par son prénom, présente-toi brièvement, "
                f"puis explique en une phrase comment fonctionne le bouton: "
                f"'Maintiens le bouton appuyé pour parler, relâche quand tu as terminé.' "
                f"Termine en lui demandant ce qu'on va travailler aujourd'hui. "
                f"2–3 phrases maximum. Sois chaleureuse et enthousiaste."
            )
            fallback = (
                f"Bonjour {name}! Je suis Mme Nova, ta tutrice de maths. "
                f"Pour me parler, maintiens le bouton appuyé et relâche quand tu as terminé. "
                f"Alors, sur quel sujet veux-tu travailler aujourd'hui?"
            )
        else:
            intro_prompt = (
                f"You are starting a new tutoring session with {name}. "
                f"Welcome them warmly by name, briefly introduce yourself as Ms. Nova, "
                f"then explain the push-to-talk button in one sentence: "
                f"'Hold the button to talk, release when you're done.' "
                f"Then ask what math topic they'd like to work on today. "
                f"2–3 sentences maximum. Be warm and enthusiastic."
            )
            fallback = (
                f"Hi {name}, I'm Ms. Nova — great to meet you! "
                f"Hold the button to talk, release when you're done. "
                f"What math topic would you like to work on today?"
            )

        if _llm_client is None:
            intro_text = fallback
            log.warning(f"[{req.session_id}] No LLM + no topic — using hardcoded open-ended intro.")
        else:
            try:
                intro_text = await _call_llm(
                    system,
                    [{"role": "user", "content": intro_prompt}],
                    req.session_id,
                )
            except Exception as e:
                log.error(f"[{req.session_id}] Intro LLM error: {e}")
                intro_text = fallback

    # ── Step 4: Store intro as first assistant message ────────────────────────
    if _redis_client:
        history = [{"role": "assistant", "content": intro_text}]
        key = f"nova:session:{req.session_id}:history"
        try:
            await _redis_client.set(key, json.dumps(history), ex=SESSION_TTL_SEC)
        except Exception as e:
            log.warning(f"[{req.session_id}] Failed to store intro history: {e}")

    log.info(f"[{req.session_id}] Intro sent to {name}: \"{intro_text[:100]}\"")

    # When resuming and a problem was pre-loaded, attach it to the response.
    # Node.js will send whiteboard_update + question_display events before TTS
    # so the child sees the problem the moment Nova starts speaking.
    if is_resuming and resume_problem:
        return ChatResponse(
            response_text      = intro_text,
            animation_emotion  = "Talking",
            whiteboard_problem = resume_problem["text"],
            whiteboard_steps   = resume_wb_steps,
            question_display   = resume_qd,
            chunk_phase        = resume_chunk,
        )

    return ChatResponse(response_text=intro_text, animation_emotion="Talking")


@app.post("/session/checkpoint")
async def session_checkpoint(req: SessionEndRequest):
    """
    Save a resume checkpoint for the current session WITHOUT ending it.
    Called by Node.js when Unity sends a session_pause event (app goes to
    background / is killed mid-session).

    The session Redis keys (history, profile, curriculum) are left intact so
    the session can continue if the app returns. Only the long-lived
    nova:child:{id}:topic:{key}:checkpoint key is written/refreshed.

    Uses the same SessionEndRequest shape (just needs session_id).
    """
    if _redis_client:
        curr_key    = f"nova:session:{req.session_id}:curriculum"
        profile_key = f"nova:session:{req.session_id}:profile"
        try:
            raw = await _redis_client.get(curr_key)
            if raw:
                cs = CurriculumState.from_dict(json.loads(raw))
                if cs.topic_key:
                    profile_raw = await _redis_client.get(profile_key)
                    if profile_raw:
                        profile = ChildProfile.model_validate_json(profile_raw)
                        await _save_checkpoint(profile.child_id, cs.topic_key, cs)
                        log.info(
                            f"[{req.session_id}] Mid-session checkpoint saved — "
                            f"child={profile.child_id}, topic={cs.topic_key}, "
                            f"problems_seen={len(cs.problems_seen)}"
                        )
                        return {"status": "ok", "problems_seen": len(cs.problems_seen)}
        except Exception as e:
            log.warning(f"[{req.session_id}] Checkpoint save failed: {e}")

    return {"status": "no_data"}


@app.post("/session/end")
async def session_end(req: SessionEndRequest):
    """
    Clean up Redis keys for the session.
    Called by Node.js when session_end event arrives.
    Returns session_correct + session_total so the WSS can include them
    in the session_summary event for Unity's mastery calculation.
    """
    session_correct, session_total = 0, 0

    if _redis_client:
        # Read curriculum stats before deleting keys
        curr_key    = f"nova:session:{req.session_id}:curriculum"
        profile_key = f"nova:session:{req.session_id}:profile"
        try:
            raw = await _redis_client.get(curr_key)
            if raw:
                cs = CurriculumState.from_dict(json.loads(raw))
                session_correct = cs.session_correct
                session_total   = cs.session_total

                # ── Save resume checkpoint ────────────────────────────────────
                # Persist problems_seen + difficulty so the child continues from
                # here when they return to this topic in a future session.
                profile_raw = await _redis_client.get(profile_key)
                if profile_raw and cs.topic_key:
                    try:
                        profile = ChildProfile.model_validate_json(profile_raw)
                        await _save_checkpoint(profile.child_id, cs.topic_key, cs)
                    except Exception as cp_err:
                        log.warning(f"[{req.session_id}] Checkpoint save failed: {cp_err}")

        except Exception as e:
            log.warning(f"[{req.session_id}] Failed to read curriculum end stats: {e}")

        keys = [
            f"nova:session:{req.session_id}:history",
            f"nova:session:{req.session_id}:profile",
            f"nova:session:{req.session_id}:curriculum",
            f"nova:session:{req.session_id}:topic",
        ]
        try:
            await _redis_client.delete(*keys)
        except Exception as e:
            log.warning(f"[{req.session_id}] Redis cleanup failed: {e}")

    log.info(
        f"[{req.session_id}] Session ended — "
        f"correct: {session_correct}/{session_total} — Redis keys cleaned up."
    )
    return {
        "status":          "ok",
        "session_correct": session_correct,
        "session_total":   session_total,
    }


# ── Answer feedback phrases ────────────────────────────────────────────────────
# Hardcoded praise for correct answers — no LLM needed, low latency.
_CORRECT_EN = [
    "That's exactly right — great work! Let's keep going.",
    "Perfect! You nailed it. Nice thinking!",
    "Correct! Excellent! You're really getting this.",
    "Yes! That's the one. Well done — let's move on.",
    "Brilliant! You got it! I love the confidence.",
]
_CORRECT_FR = [
    "C'est exactement ça — excellent travail! On continue.",
    "Parfait! Tu as trouvé. Beau raisonnement!",
    "Correct! Tu maîtrises vraiment bien ça.",
    "Oui! C'est la bonne réponse. Bravo — on avance.",
    "Brillant! Tu l'as eu! J'adore ta confiance.",
]


@app.post("/answer", response_model=ChatResponse)
async def record_answer(req: AnswerRequest):
    """
    Record a tap-to-answer result from Unity's question card.

    Steps:
      1. Increment session_correct / session_total in CurriculumState (Redis).
      2. Generate Ms. Nova's verbal reaction:
           • Correct  → random hardcoded praise (fast, no LLM).
           • Wrong    → LLM-generated explanation using session history so Nova
                        references the actual question and correct answer.
      3. Append Nova's reaction to session history so the next /chat turn has
         full context (Nova already acknowledged the answer).
      4. Return ChatResponse so Node.js can synthesize TTS and send nova_speaking.
    """
    # ── Step 1: Update curriculum stats ──────────────────────────────────────
    if _redis_client:
        curr_key = f"nova:session:{req.session_id}:curriculum"
        try:
            raw = await _redis_client.get(curr_key)
            if raw:
                cs = CurriculumState.from_dict(json.loads(raw))
                cs.session_total += 1
                if req.is_correct:
                    cs.session_correct += 1
                cs.mc_answered = True   # signal to node_select_pedagogy: don't double-count
                await _redis_client.set(
                    curr_key, json.dumps(cs.to_dict()), ex=SESSION_TTL_SEC
                )
                log.info(
                    f"[{req.session_id}] Answer recorded: "
                    f"{'✓ correct' if req.is_correct else '✗ wrong'} "
                    f"({cs.session_correct}/{cs.session_total})"
                )
        except Exception as e:
            log.warning(f"[{req.session_id}] Failed to record answer: {e}")

    # ── Step 2: Load profile to pick language ─────────────────────────────────
    profile: Optional[ChildProfile] = None
    if _redis_client:
        try:
            raw = await _redis_client.get(f"nova:session:{req.session_id}:profile")
            if raw:
                profile = ChildProfile.model_validate_json(raw)
        except Exception:
            pass

    lang = profile.language if profile else "en"

    # ── Step 3: Generate Nova's reaction ──────────────────────────────────────
    if req.is_correct:
        # Hardcoded praise — fast, warm, varied
        phrases = _CORRECT_FR if lang == "fr" else _CORRECT_EN
        response_text = random.choice(phrases)
        animation     = "Celebrating"
        log.info(f"[{req.session_id}] Correct answer — using praise phrase.")

    else:
        # Wrong answer — LLM generates a targeted explanation so Nova addresses
        # the specific mistake rather than giving a generic "try again".
        animation = "Concerned"
        fallback_en = (
            "Not quite — but that's okay! Look at the correct answer I've highlighted. "
            "Let's think through why that one works, and you'll get the next one!"
        )
        fallback_fr = (
            "Pas tout à fait — mais c'est normal! Regarde la bonne réponse que j'ai mise en évidence. "
            "Réfléchissons pourquoi celle-là est correcte, et tu réussiras la prochaine!"
        )
        fallback = fallback_fr if lang == "fr" else fallback_en

        if _llm_client is None or profile is None:
            response_text = fallback
            log.warning(f"[{req.session_id}] Wrong answer — no LLM/profile, using fallback.")
        else:
            try:
                history = await _load_history(req.session_id)
                system  = build_system_prompt(profile)

                if lang == "fr":
                    explain_prompt = (
                        "L'élève vient de donner une mauvaise réponse à la question ci-dessus. "
                        "En 2-3 phrases courtes: (1) reconnais gentiment l'erreur, "
                        "(2) explique clairement pourquoi la bonne réponse est correcte, "
                        "(3) termine par un encouragement bref. "
                        "Ne pose pas encore de nouvelle question."
                    )
                else:
                    explain_prompt = (
                        "The student just gave an incorrect answer to the question above. "
                        "In 2-3 short sentences: (1) gently acknowledge the mistake, "
                        "(2) explain clearly why the correct answer is right, "
                        "(3) end with a brief word of encouragement. "
                        "Do NOT ask a new question yet."
                    )

                response_text = await _call_llm(
                    system,
                    history + [{"role": "user", "content": explain_prompt}],
                    req.session_id,
                )
                log.info(f"[{req.session_id}] Wrong answer — LLM explanation generated.")
            except Exception as e:
                log.error(f"[{req.session_id}] Wrong-answer LLM failed: {e}")
                response_text = fallback

    # ── Step 4: Append Nova's reaction to history ─────────────────────────────
    if _redis_client:
        try:
            hist_key = f"nova:session:{req.session_id}:history"
            history  = await _load_history(req.session_id)
            history.append({"role": "assistant", "content": response_text})
            # Keep last 20 messages
            if len(history) > 20:
                history = history[-20:]
            await _redis_client.set(hist_key, json.dumps(history), ex=SESSION_TTL_SEC)
        except Exception as e:
            log.warning(f"[{req.session_id}] Failed to update history after answer: {e}")

    return ChatResponse(
        response_text     = response_text,
        animation_emotion = animation,
    )


@app.post("/session/continue", response_model=ChatResponse)
async def session_continue(req: SessionContinueRequest):
    """
    Called by Node.js after a correct or wrong MC answer response has been spoken.
    Automatically advances the curriculum (loads next problem) and generates
    Ms. Nova's natural transition text so the lesson continues without the child
    having to press PTT.

    This endpoint does NOT add a synthetic user message to history, keeping the
    conversation history clean.
    """
    # ── Load and advance curriculum state ────────────────────────────────────
    cs_dict = await _load_curriculum_state(req.session_id)
    cs      = CurriculumState.from_dict(cs_dict) if cs_dict else CurriculumState()

    # Load child profile for language and grade
    profile: Optional[ChildProfile] = None
    if _redis_client:
        try:
            raw = await _redis_client.get(f"nova:session:{req.session_id}:profile")
            if raw:
                profile = ChildProfile.model_validate_json(raw)
        except Exception:
            pass
    if profile is None:
        profile = ChildProfile(child_id="unknown")

    lang = profile.language

    # ── Advance to next problem ───────────────────────────────────────────────
    prev_problem_text = cs.problem_text  # for transition context
    advanced = False

    if _curriculum and cs.has_problem:
        # mc_answered was set by /answer; is_exhausted is now True
        new_difficulty = _curriculum.next_difficulty(
            cs.session_correct, cs.session_total, cs.difficulty
        )
        cs.difficulty  = new_difficulty
        cs.mc_answered = False   # consumed here

        problem = await _curriculum.select_problem(
            topic_key   = cs.topic_key or _curriculum._fallback_topic_key(profile.grade),
            grade       = profile.grade,
            language    = profile.language,
            difficulty  = cs.difficulty,
            exclude_ids = cs.problems_seen,
        )
        if problem:
            cs.load_problem(problem)
            advanced = True
            log.info(
                f"[{req.session_id}] session/continue — advanced to next problem: "
                f"{problem.id[:8]} diff={problem.difficulty}"
            )
        else:
            log.warning(f"[{req.session_id}] session/continue — no unseen problems available.")

    # Persist updated curriculum state
    if _redis_client:
        try:
            await _redis_client.set(
                f"nova:session:{req.session_id}:curriculum",
                json.dumps(cs.to_dict()),
                ex=SESSION_TTL_SEC,
            )
        except Exception as e:
            log.warning(f"[{req.session_id}] session/continue — Redis write failed: {e}")

    # ── Generate transition text ──────────────────────────────────────────────
    history = await _load_history(req.session_id)

    if advanced and cs.has_problem:
        # Tell Nova to transition from the last answer to the new problem
        if lang == "fr":
            if req.is_correct:
                transition_note = (
                    f"L'élève vient de répondre correctement. "
                    f"Maintenant, présente le prochain problème de façon naturelle et enthousiaste. "
                    f"Problème: {cs.problem_text}. "
                    f"1-2 phrases max. Pose une question d'ouverture sur ce problème."
                )
            else:
                transition_note = (
                    f"L'élève vient d'avoir une erreur et Nova a expliqué. "
                    f"Passons maintenant au prochain problème pour continuer à progresser. "
                    f"Problème: {cs.problem_text}. "
                    f"1-2 phrases max. Introduis-le chaleureusement."
                )
        else:
            if req.is_correct:
                transition_note = (
                    f"The student just answered correctly. "
                    f"Transition warmly and naturally to the next problem. "
                    f"Problem: {cs.problem_text}. "
                    f"1-2 sentences max. Ask an opening question about this problem."
                )
            else:
                transition_note = (
                    f"The student got the last question wrong and Nova explained it. "
                    f"Move on to the next problem to keep momentum. "
                    f"Problem: {cs.problem_text}. "
                    f"1-2 sentences max. Introduce it warmly."
                )

        system = build_system_prompt(profile)
        system += f"\n\n[Internal note — do NOT reveal to student]\n{transition_note}"

        if lang == "fr":
            default_text = (
                f"Super, passons à la suite! "
                f"{cs.problem_text} — qu'est-ce que tu penses pour commencer?"
            )
        else:
            default_text = (
                f"Great, let's keep going! "
                f"{cs.problem_text} — what do you think the first step is?"
            )

        if _llm_client:
            try:
                # Use history for context but send an empty user turn as trigger
                response_text = await _call_llm(
                    system,
                    history + [{"role": "user", "content": "[continue]"}],
                    req.session_id,
                )
            except Exception as e:
                log.error(f"[{req.session_id}] session/continue LLM error: {e}")
                response_text = default_text
        else:
            response_text = default_text

    else:
        # No new problem available — wrap up the session topic
        if lang == "fr":
            response_text = (
                "Tu as fait un excellent travail aujourd'hui! "
                "On a couvert tous les problèmes disponibles pour ce sujet. "
                "Dis-moi ce que tu as trouvé le plus intéressant!"
            )
        else:
            response_text = (
                "Excellent work today! "
                "You've worked through all the available problems on this topic. "
                "Tell me what you found most interesting!"
            )

    # ── Append transition to history (as assistant only — no synthetic user msg) ─
    if _redis_client:
        try:
            updated_history = list(history)
            updated_history.append({"role": "assistant", "content": response_text})
            if len(updated_history) > 20:
                updated_history = updated_history[-20:]
            await _redis_client.set(
                f"nova:session:{req.session_id}:history",
                json.dumps(updated_history),
                ex=SESSION_TTL_SEC,
            )
        except Exception as e:
            log.warning(f"[{req.session_id}] session/continue history write failed: {e}")

    animation = "Celebrating" if req.is_correct else "Encouraging"

    return ChatResponse(
        response_text      = response_text,
        animation_emotion  = animation,
        whiteboard_problem = cs.problem_text if (advanced and cs.has_problem) else None,
        whiteboard_steps   = cs.solution_steps if (advanced and cs.has_problem) else [],
        question_display   = CurriculumEngine.build_question_data(cs) if (advanced and cs.has_problem) else None,
        chunk_phase        = cs.chunk_phase,
    )


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
        "question_display":   None,
        "chunk_phase":        "intro",
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
        question_display   = final_state.get("question_display"),
        chunk_phase        = final_state.get("chunk_phase", "intro"),
    )
