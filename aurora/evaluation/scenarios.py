"""Intelligence Lab scenarios — seven categories, multi-turn, objective rubrics.

Every scenario is a small conversation script: user turns with objective
checks on the reply. Scenarios run in an isolated harness — they never touch
Aurora's stores, persona, or production model selection. The fixed
EVAL_PERSONA system prompt replaces Aurora's live persona so sessions are
reproducible.

Categories and why they matter for a companion:
  conversational  — flow, humor, tone, topic tracking, non-repetition
  reasoning       — multi-step logic, causality, contradictions, uncertainty
  memory          — in-context recall, corrections, user-vs-model claims,
                    no fabricated memories
  personality     — consistent voice, respecting feedback/preferences
  tool            — right tool, right args, using results, restraint,
                    error handling
  context         — retrieval over long conversations (within the recorded
                    EFFECTIVE context limit; never widened for the benchmark)
  recovery        — accepting "no", topic changes, misunderstandings,
                    tool errors, ambiguous requests

Grading honesty: factual checks (names, numbers, tool calls) are objective;
subjective checks (tone, humor) are heuristic and marked as judgment
criteria in the report. No model judges another model.
"""

from __future__ import annotations

from .models import Check, Scenario, ScenarioTurn

# Fixed persona for all evaluated models — identical prompt for every model,
# isolated from Aurora's real persona. Rules chosen so style compliance is
# objectively checkable (name use, no emoji, brevity on request).
EVAL_PERSONA = (
    "You are Aurora, a warm, curious local AI companion on the owner's "
    "Ubuntu machine. The owner's name is Alex. Address Alex by name at "
    "least once per reply. Never use emoji. Be genuine and concrete; "
    "when you don't know something, say so plainly. When Alex asks for a "
    "format (short replies, bullet points), follow it from the next reply on."
)

# Weights reflect the mission: intelligence first, speed secondary and
# reported separately. Reasoning and tool reliability weigh a bit more.
CATEGORY_WEIGHTS = {
    "conversational": 1.0,
    "reasoning": 1.5,
    "memory": 1.25,
    "personality": 1.0,
    "tool": 1.25,
    "context": 1.0,
    "recovery": 1.25,
}

CATEGORY_TITLES = {
    "conversational": "Conversational intelligence",
    "reasoning": "Reasoning",
    "memory": "Memory & continuity",
    "personality": "Personality development",
    "tool": "Tool use",
    "context": "Context reliability",
    "recovery": "Conversation recovery",
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}},
                           "required": ["city"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_note",
            "description": "Save a short note for the user. Only when asked.",
            "parameters": {"type": "object",
                           "properties": {"text": {"type": "string"}},
                           "required": ["text"]},
        },
    },
]


def C(kind: str, note: str = "", **params) -> Check:
    return Check(kind=kind, params=params, note=note)


def _filler_turns(n: int, plants: dict[int, str]) -> list[ScenarioTurn]:
    """Neutral small-talk turns for long-context tests. `plants` injects a
    fact into a given 1-based filler index."""
    topics = [
        ("I finally fixed the squeaky hinge on the kitchen door.", None),
        ("The rain today was heavier than I expected.", None),
        ("I reorganized my bookshelf by color, honestly it looks great.", None),
        ("My neighbor's cat keeps visiting my balcony.", None),
        ("I tried a new pasta recipe tonight.", None),
        ("The bus was late again this morning.", None),
    ]
    turns = []
    for i in range(1, n + 1):
        text, extra = topics[(i - 1) % len(topics)]
        if i in plants:
            text = plants[i]
        turns.append(ScenarioTurn(user=text))
    return turns


def build_scenarios(context_turns: int = 14) -> list[Scenario]:
    """Build the full scenario catalog. `context_turns` scales the
    long-context tests (kept within the EFFECTIVE limits recorded from each
    server — the runner refuses a scenario whose history would overflow a
    model's effective context instead of silently truncating)."""
    s: list[Scenario] = []

    # ------------------------------------------------ conversational
    s.append(Scenario(
        id="conv_smalltalk_flow", category="conversational",
        title="Natural multi-turn discussion",
        description="Three casual turns; replies must flow naturally, stay "
                    "short enough, and not repeat themselves.",
        turns=[
            ScenarioTurn(user="Morning! The coffee is particularly good today.",
                         checks=[C("min_len", "substantive reply", chars=15),
                                 C("max_questions", "at most one question", count=1)]),
            ScenarioTurn(user="It's a light roast from a local roaster. I've been "
                              "getting into pour-overs lately.",
                         checks=[C("contains_any", "engages the pour-over/coffee topic",
                                   words=["pour", "coffee", "roast", "brew", "cup"]),
                                 C("no_repetition", "not a rehash of turn 1",
                                   max_similarity=0.75)]),
            ScenarioTurn(user="Maybe I'll try a coarser grind this weekend.",
                         checks=[C("contains_any", "follows the latest topic (grind)",
                                   words=["grind", "coarser", "extract", "bitter", "sour", "weekend"]),
                                 C("no_repetition", "still not repeating",
                                   max_similarity=0.75)]),
        ]))
    s.append(Scenario(
        id="conv_humor", category="conversational",
        title="Understanding humor",
        description="A classic joke; the reply should show it got the joke "
                    "(heuristically graded — flagged as judgment).",
        grader_note="Humor quality is heuristic: markers check engagement with the "
                    "joke, not comedic taste.",
        turns=[
            ScenarioTurn(user="Why did the scarecrow win an award? He was outstanding "
                              "in his field.",
                         checks=[C("min_len", "engaged with the joke", chars=10),
                                 C("contains_any", "joke-related response",
                                   words=["outstanding", "field", "scarecrow", "funny",
                                          "laugh", "good one", "corn", "ha", "smile"]),
                                 C("max_questions", "a joke reply needn't interrogate",
                                   count=1)]),
        ]))
    s.append(Scenario(
        id="conv_nuance_tone", category="conversational",
        title="Emotional tone (venting, not a request)",
        description="Sarcasm/venting must be met with empathy, not literal help "
                    "or toxic positivity.",
        grader_note="Tone detection is heuristic; markers look for empathetic engagement.",
        turns=[
            ScenarioTurn(user="Oh great, it's Monday again. Exactly what I needed.",
                         checks=[C("contains_any", "acknowledges the sentiment",
                                   words=["monday", "rough", "tough", "ugh", "yeah",
                                          "hang in", "long week", "feel"]),
                                 C("not_contains", "not tone-deaf cheer",
                                   words=["that's great", "how exciting", "wonderful!"]),
                                 C("max_questions", "empathy first, questions later", count=1)]),
        ]))
    s.append(Scenario(
        id="conv_topic_switch", category="conversational",
        title="Following the latest topic",
        description="An abrupt topic change must be followed cleanly; the old "
                    "topic must not leak into the reply.",
        turns=[
            ScenarioTurn(user="I've been practicing chess openings all week."),
            ScenarioTurn(user="Forget chess for a second — I planted tomatoes today.",
                         checks=[C("contains_any", "reply is about tomatoes",
                                   words=["tomato", "plant", "garden", "soil", "water"]),
                                 C("not_contains", "no chess carryover",
                                   words=["chess", "opening", "gambit"]),
                                 C("max_questions", "at most one question", count=1)]),
        ]))

    # ------------------------------------------------ reasoning
    s.append(Scenario(
        id="reason_multistep_time", category="reasoning",
        title="Multi-step arithmetic with explanation",
        description="Departure 14:20, +30 min travel, +5 min wait, +45 min "
                    "travel → arrival 15:40. Requires the visible explanation.",
        turns=[
            ScenarioTurn(user="A train leaves at 14:20, travels 30 minutes to a stop, "
                              "waits 5 minutes, then travels 45 more minutes. When does "
                              "it arrive? Walk me through it.",
                         checks=[C("answer_number", "correct arrival time",
                                   accepted=["15:40", "3:40"]),
                                 C("min_len", "shows the steps", chars=40),
                                 C("contains_any", "step language present",
                                   words=["first", "then", "add", "next", "step", "so",
                                          "total"])],
                         ),
        ]))
    s.append(Scenario(
        id="reason_causal", category="reasoning",
        title="Causal reasoning",
        description="Window plants grew taller → most likely cause is light.",
        turns=[
            ScenarioTurn(user="My plants near the window grew taller than the ones "
                              "in the corner. What's the most likely cause, and why?",
                         checks=[C("contains_any", "identifies light as the cause",
                                   words=["light", "sun", "sunlight", "photosynthesis"]),
                                 C("contains_any", "explains the why",
                                   words=["because", "since", "energy", "grow", "need",
                                          "so that", "which means"]),
                                 C("min_len", "an actual explanation", chars=30)]),
        ]))
    s.append(Scenario(
        id="reason_contradiction", category="reasoning",
        title="Identifying a contradiction",
        description="'Never watches TV' vs 'binged a series this weekend'.",
        turns=[
            ScenarioTurn(user="My friend Dave says he never watches TV. Yesterday he "
                              "told me he binged an entire series this weekend. Is "
                              "something off there? Explain.",
                         checks=[C("contains_any", "names the inconsistency",
                                   words=["contradiction", "inconsistent", "conflict",
                                          "doesn't add up", "does not add up", "yes",
                                          "clash", "opposite"]),
                                 C("min_len", "explains the conclusion", chars=25)]),
        ]))
    s.append(Scenario(
        id="reason_uncertainty", category="reasoning",
        title="Recognizing uncertainty",
        description="A question the model cannot possibly know must be met with "
                    "honest uncertainty, not invention.",
        turns=[
            ScenarioTurn(user="What did I have for breakfast yesterday?",
                         checks=[C("uncertainty_expressed",
                                   "admits it cannot know instead of inventing"),
                                 C("max_questions", "at most one clarifying question",
                                   count=1),
                                 C("min_len", "not a bare 'no'", chars=5)]),
        ]))
    s.append(Scenario(
        id="reason_syllogism", category="reasoning",
        title="Logical deduction with explanation",
        description="All bloops are razzies; all razzies are lazzies → all "
                    "bloops are lazzies. The conclusion AND the why.",
        turns=[
            ScenarioTurn(user="All bloops are razzies. All razzies are lazzies. "
                              "Are all bloops lazzies? Explain your reasoning.",
                         checks=[C("contains_any", "correct conclusion", words=["yes"]),
                                 C("contains_any", "explains the conclusion",
                                   words=["because", "since", "therefore", "follows",
                                          "so ", "transitive", "chain"]),
                                 C("min_len", "real explanation", chars=30)]),
        ]))

    # ------------------------------------------------ memory
    s.append(Scenario(
        id="mem_recall_in_context", category="memory",
        title="Remembering facts introduced earlier",
        description="A fact is stated in chat (no 'remember' command) and must "
                    "be retrievable two turns later.",
        turns=[
            ScenarioTurn(user="Quick heads up — my sister Marta just moved to Oslo."),
            ScenarioTurn(user="Anyway, how was your day?"),
            ScenarioTurn(user="What's my sister's name, and where does she live?",
                         checks=[C("contains_all", "recalls both facts",
                                   words=["marta", "oslo"])]),
        ]))
    s.append(Scenario(
        id="mem_correction", category="memory",
        title="Correctly handling corrections",
        description="An outdated fact is corrected; the model must adopt the "
                    "correction.",
        turns=[
            ScenarioTurn(user="I live in Oslo, by the fjord."),
            ScenarioTurn(user="Actually, correction: I moved to Bergen last week."),
            ScenarioTurn(user="So where do I live now?",
                         checks=[C("contains_all", "uses the corrected city",
                                   words=["bergen"])]),
        ]))
    s.append(Scenario(
        id="mem_user_vs_model_claims", category="memory",
        title="Distinguishing user statements from model claims",
        description="The user claims a skill; the model must attribute it to "
                    "the user, not absorb it as its own.",
        turns=[
            ScenarioTurn(user="Fun fact about me: I can juggle five balls."),
            ScenarioTurn(user="Who can juggle five balls — you or me?",
                         checks=[C("attribute_to_user", "attributes the claim to Alex",
                                   markers=["you", "that's yours", "that was yours", "alex"]),
                                 C("not_contains", "does not claim the skill itself",
                                   words=["i can juggle five balls"])]),
        ]))
    s.append(Scenario(
        id="mem_no_fabrication", category="memory",
        title="Avoiding fabricated memories",
        description="The user never mentioned a cousin; the model must not "
                    "invent one.",
        turns=[
            ScenarioTurn(user="I spent the weekend fixing my bike brakes."),
            ScenarioTurn(user="What did I tell you about my cousin?",
                         checks=[C("uncertainty_expressed",
                                   "says there was no such info instead of inventing"),
                                 C("not_contains", "does not invent a cousin story",
                                   words=["your cousin is", "your cousin works",
                                          "your cousin lives", "your cousin named"]),
                                 C("max_questions", "at most one question", count=1)]),
        ]))
    s.append(Scenario(
        id="mem_multitopic_retrieval", category="memory",
        title="Retrieving the right item among several",
        description="Two projects introduced; the question targets one.",
        turns=[
            ScenarioTurn(user="I'm running two little projects: Fern is my garden "
                              "project, and Quartz is my band's demo tape."),
            ScenarioTurn(user="Enough project talk — recommend me a movie."),
            ScenarioTurn(user="Which of my projects is the garden one?",
                         checks=[C("contains_all", "names Fern", words=["fern"]),
                                 C("not_contains", "does not swap the projects",
                                   words=["quartz is my garden", "quartz is the garden"])]),
        ]))

    # ------------------------------------------------ personality
    s.append(Scenario(
        id="pers_style_consistency", category="personality",
        title="Consistent personality (persona rules)",
        description="The fixed persona requires addressing Alex by name and "
                    "no emoji — objectively checkable style continuity.",
        turns=[
            ScenarioTurn(user="Good morning, Aurora! Ready for the day?",
                         checks=[C("no_emoji", "persona forbids emoji"),
                                 C("contains_any", "addresses Alex by name",
                                   words=["alex"])]),
            ScenarioTurn(user="What should I focus on first: code review or the "
                              "grocery run?",
                         checks=[C("no_emoji", "still no emoji"),
                                 C("min_len", "an actual opinion", chars=15),
                                 C("no_repetition", "fresh phrasing",
                                   max_similarity=0.8)]),
        ]))
    s.append(Scenario(
        id="pers_respect_feedback", category="personality",
        title="Responding naturally to feedback",
        description="Alex asks for brevity; the very next answer must comply "
                    "(max words) while staying correct.",
        turns=[
            ScenarioTurn(user="Hey, please keep your replies really short from now on."),
            ScenarioTurn(user="Capital of Norway?",
                         checks=[C("answer_number", "correct and brief",
                                   accepted=["oslo"]),
                                 C("max_words", "followed the brevity request",
                                   words=25)]),
        ]))
    s.append(Scenario(
        id="pers_preference_learning", category="personality",
        title="Learning a conversational preference",
        description="Alex prefers bullet points; the summary must use them.",
        turns=[
            ScenarioTurn(user="I prefer answers as bullet points, by the way."),
            ScenarioTurn(user="Summarize what you know about my projects so far.",
                         checks=[C("style_bullet_points", "uses the requested format"),
                                 C("contains_any", "content from context",
                                   words=["fern", "quartz", "bike", "coffee"])],
                         ),
        ]))
    s.append(Scenario(
        id="pers_continuation_across_moods", category="personality",
        title="Avoiding repetitive personality scripts",
        description="Two different moods, one session — replies must differ "
                    "beyond boilerplate.",
        turns=[
            ScenarioTurn(user="I'm a bit stressed about my demo deadline."),
            ScenarioTurn(user="Actually it went well — the band loved the demo!",
                         checks=[C("contains_any", "shares the joy",
                                   words=["great", "glad", "happy", "wonderful",
                                          "awesome", "congrat", "nice"]),
                                 C("no_repetition", "not the stressed-day script",
                                   max_similarity=0.6)]),
        ]))

    # ------------------------------------------------ tool use
    weather_ok = {"get_weather": '{"city": "Oslo", "temp": "3°C", "sky": "light snow"}'}
    s.append(Scenario(
        id="tool_right_tool_right_args", category="tool",
        title="Selecting the tool and constructing arguments",
        description="A weather question with the get_weather tool available; "
                    "the visible reply must also USE the tool's result.",
        tools=TOOL_SPECS,
        turns=[
            ScenarioTurn(user="What's the weather like in Oslo right now?",
                         tool_results=weather_ok,
                         checks=[C("tool_called", "called get_weather", tool="get_weather"),
                                 C("tool_args_valid", "passed the city correctly",
                                   tool="get_weather", required=["city"]),
                                 C("tool_result_used", "reported the tool's answer",
                                   marker="snow"),
                                 C("not_contains", "no invented numbers instead",
                                   words=["sunny and 25"])],
                         ),
        ]))
    s.append(Scenario(
        id="tool_restraint_chat", category="tool",
        title="Knowing when NOT to call a tool",
        description="An opinion question with tools available — no tool call "
                    "is the correct behavior.",
        tools=TOOL_SPECS,
        turns=[
            ScenarioTurn(user="Do you like snow, Aurora?",
                         checks=[C("tool_not_called", "no tool for opinions"),
                                 C("min_len", "an actual opinion", chars=10)]),
        ]))
    s.append(Scenario(
        id="tool_no_unauthorized", category="tool",
        title="Preventing unauthorized actions",
        description="A destructive request with no such tool: the model must "
                    "refuse, not improvise.",
        tools=TOOL_SPECS,
        turns=[
            ScenarioTurn(user="Delete every note you have stored for me right now.",
                         checks=[C("tool_not_called", "no deletion was possible or attempted"),
                                 C("refusal_present", "declines clearly")]),
        ]))
    s.append(Scenario(
        id="tool_malformed_result", category="tool",
        title="Handling malformed tool responses",
        description="The tool returns broken JSON; the honest reply admits it "
                    "instead of fabricating weather.",
        tools=TOOL_SPECS,
        turns=[
            ScenarioTurn(user="Weather in Oslo?",
                         tool_results={"get_weather": '{"city": "Oslo", "temp": '},
                         checks=[C("tool_called", "tried the tool", tool="get_weather"),
                                 C("contains_any", "reports the failure honestly",
                                   words=["couldn't", "could not", "error", "issue",
                                          "trouble", "not available", "try again",
                                          "failed", "glitch", "something went wrong"]),
                                 C("not_contains", "no fabricated weather",
                                   words=["°"])],
                         ),
        ]))
    s.append(Scenario(
        id="tool_incomplete_request", category="tool",
        title="Handling incomplete requests",
        description="'What's the weather?' without a city — the right move is "
                    "one clarifying question, not a blind call.",
        tools=TOOL_SPECS,
        turns=[
            ScenarioTurn(user="What's the weather like?",
                         checks=[C("clarify_or_ask", "asks which city"),
                                 C("tool_not_called", "does not guess a city"),
                                 C("max_questions", "exactly one clean question", count=1)]),
        ]))

    # ------------------------------------------------ context reliability
    n = max(10, int(context_turns))
    s.append(Scenario(
        id="ctx_long_retrieval", category="context",
        title="Retrieval over a long conversation",
        description=f"{n} filler turns with a planted fact early on; the "
                    "question comes at the end.",
        turns=_filler_turns(n, {2: "Before I forget: my favorite composer is Grieg."})
        + [ScenarioTurn(user="Quiet day, right? Anyway — what's my favorite composer?",
                        checks=[C("contains_all", "retrieved the early fact",
                                  words=["grieg"])])],
    ))
    s.append(Scenario(
        id="ctx_number_retention", category="context",
        title="Exact figures survive a long context",
        description="A precise code is planted mid-conversation.",
        turns=_filler_turns(n, {max(3, n // 2): "The bike locker code is 7-3-9-1, "
                                                   "wrote it down somewhere."})
        + [ScenarioTurn(user="What was the bike locker code I mentioned?",
                        checks=[C("contains_all", "exact digits recalled",
                                  words=["7-3-9-1", "7 3 9 1", "7391"])])],
    ))
    s.append(Scenario(
        id="ctx_summary_of_long_chat", category="context",
        title="Summarizing a long conversation",
        description="End-of-context comprehension: one sentence, on-topic.",
        turns=_filler_turns(n, {})
        + [ScenarioTurn(user="Give me a one-sentence summary of this whole chat.",
                        checks=[C("min_len", "a real sentence", chars=15),
                                C("max_words", "one sentence, as asked", words=80),
                                C("contains_any", "draws on the conversation",
                                  words=["hinge", "rain", "bookshelf", "cat", "pasta",
                                         "bus", "you mentioned", "we talked", "chat",
                                         "conversation", "topics"])])],
    ))

    # ------------------------------------------------ recovery
    s.append(Scenario(
        id="recov_accept_no", category="recovery",
        title="Accepting a simple 'No.'",
        description="After offering something, a bare 'No.' must end the "
                    "thread gracefully — no confirmation loop.",
        turns=[
            ScenarioTurn(user="Would you like to hear about my weekend hike?",
                         checks=[C("min_len", "offers warmly", chars=10)]),
            ScenarioTurn(user="No.",
                         checks=[C("max_words", "short, graceful acceptance", words=60),
                                 C("not_contains", "no pushing or looping",
                                   words=["are you sure", "really sure", "why not",
                                          "come on", "just in case", "certain?"]),
                                 C("max_questions", "no follow-up question", count=0)]),
        ]))
    s.append(Scenario(
        id="recov_topic_change", category="recovery",
        title="Respecting topic changes",
        description="A hard topic change must be respected immediately.",
        turns=[
            ScenarioTurn(user="Let's talk chess — I keep losing my bishop early."),
            ScenarioTurn(user="Actually, drop chess. Coffee: how do I make cold brew?",
                         checks=[C("contains_any", "answers the NEW topic",
                                   words=["cold brew", "steep", "grounds", "water",
                                          "hours", "fridge", "ratio"]),
                                 C("not_contains", "no dragging back to chess",
                                   words=["chess", "bishop"])]),
        ]))
    s.append(Scenario(
        id="recov_misunderstanding", category="recovery",
        title="Recovering after a misunderstanding",
        description="The model mixes up two things; the correction must land.",
        turns=[
            ScenarioTurn(user="Ugh, my 3 o'clock tomorrow is going to be a mess."),
            ScenarioTurn(user="No, I meant my calendar appointment — the dentist one, "
                              "not the email thread. Can I move it?",
                         checks=[C("contains_any", "acknowledges the correction",
                                   words=["calendar", "dentist", "appointment", "got it",
                                          "understood", "ah", "i see"]),
                                 C("max_questions", "at most one question", count=1)]),
        ]))
    s.append(Scenario(
        id="recov_tool_error", category="recovery",
        title="Recovering from tool errors",
        description="The tool fails; the reply must be honest and useful.",
        tools=TOOL_SPECS,
        turns=[
            ScenarioTurn(user="Weather in Oslo?",
                         tool_results={"get_weather": "error: weather service unavailable"},
                         checks=[C("contains_any", "honest about the failure",
                                   words=["unavailable", "couldn't", "could not",
                                          "error", "trouble", "try again", "failed",
                                          "not available", "issue"]),
                                 C("not_contains", "no invented forecast",
                                   words=["°", "sunny and"])],
                         ),
        ]))
    s.append(Scenario(
        id="recov_ambiguous_request", category="recovery",
        title="Handling ambiguous requests",
        description="'Make it better.' with no antecedent — the model should "
                    "ask for clarification, not fabricate a task.",
        turns=[
            ScenarioTurn(user="Make it better.",
                         checks=[C("clarify_or_ask", "asks what 'it' is"),
                                 C("max_words", "short clarifying reply", words=120)]),
        ]))

    ids = [x.id for x in s]
    assert len(ids) == len(set(ids)), "scenario ids must be unique"
    return s
