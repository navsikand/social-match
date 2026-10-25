# Brainstorm — interest similarity, hard constraints, and agent negotiation

Design only. Nothing here is implemented. Two problems, both sitting on top of the shipped matcher
(`server/interests.py`, `server/matching.py`, `server/scheduling.py`).

What exists today, so we don't re-solve it:

| Layer | Today |
| --- | --- |
| Raw interest → vocabulary | `server/interests.py`: slug → exact vocab → synonym table → token/phrase match; unknown tags kept but never scored |
| Similarity | **none** — a shared canonical tag is worth `3`, everything else `0` |
| Time | window intersection, ≥30 min, longest segment wins; versioned proposals |
| Budget / distance / dietary | **do not exist** — nowhere in the payload, the schema, or the matcher |
| Negotiation | one counter round (`counter()` voids the old proposal, bumps the version), then confirm/revoke |
| Activities/events | designed, deliberately not built (FLOW §9) |

So there are really three things to add: **soft interest similarity**, **hard constraints as typed data**,
and **a bounded negotiation loop with an activity search**. ML belongs in exactly one of them.

---

## Problem 1 — interests: "espresso" vs "latte", and "climbing" vs "hiking"

## 1.1 The example you gave already works, and that's the point

`espresso` → `coffee` and `latte` → `coffee` both land in the synonym table today, so those two users share a
canonical tag and score 3. The interesting failures are the ones a synonym table can't reach:

1. **Long tail of aliases** — `cortado`, `flat white`, `macchiato`, `third wave`, `pourover`. Every real user
   invents one. A hand table has to grow forever.
2. **Siblings, not aliases** — `climbing` and `hiking` are *not the same activity*, and they are *compatible
   people*. Today they score 0.
3. **Specificity** — `coffee` is so generic it's nearly meaningless on a campus; `bouldering` is a strong
   signal. Today both are worth 3.

These are three different problems and they want three different mechanisms.

## 1.2 Options

| Option | Handles | Cost | Explainable | Verdict |
| --- | --- | --- | --- | --- |
| **A. Bigger synonym table** | aliases | small | yes | Necessary but unbounded maintenance |
| **B. Two-level taxonomy** (domain → interest) | aliases + siblings | hand-built, ~40 domains | yes, perfectly | **Core of the recommendation** |
| **C. Embeddings** (small sentence model, offline) | everything, softly | model + weights + threshold tuning; nondeterministic | poorly | Fallback only, for the tail |
| **D. Full LLM classification at ingest** | everything | per-request cost/latency, nondeterministic, needs an eval set | depends on prompt | No — it moves a deterministic guarantee into a prompt |

## 1.3 Recommendation: taxonomy first, IDF for specificity, embeddings only for the residual

**Layer 1 — taxonomy.** Give every canonical interest a parent domain and an "energy":

```text
domain: outdoors   → climbing, hiking, cycling, running, camping
domain: water      → swimming, surfing, diving
domain: racket     → tennis, badminton, squash
domain: food       → coffee, baking, cooking
domain: nightlife  → live_music, edm, techno, rock, jazz
domain: quiet      → board_games, chess, reading, photography, drawing, pottery
domain: screen     → video_games, anime, cinema
```

Two users sharing a *domain* are proposed with **partial** similarity, not full. That is the whole trick: the
taxonomy is a finite, auditable table, and every partial match can be explained in one sentence.

**Layer 2 — specificity via IDF.** Weight each canonical tag by how rare it is among active users:
`idf(t) = log(N / (1 + users_with_t))`. `coffee` (everyone) contributes almost nothing; `bouldering` (rare)
contributes a lot. This is self-tuning, deterministic, needs no training, and fixes the "generic tag inflates
everyone" problem as the user base grows. Cold start: fall back to a hand-set prior weight per tag.

**Layer 3 — embeddings, only for the residual.** If a raw interest matches nothing in vocab *or* synonyms,
embed it and take the nearest canonical tag above a similarity threshold (say 0.72). Store the mapping and how
it was produced:

```json
{"raw": "cortado", "canonical": "coffee", "method": "synonym"}
{"raw": "crimping", "canonical": "climbing", "method": "embedding", "score": 0.78}
{"raw": "mycology", "canonical": null, "method": "none"}
```

Three properties that make this safe:

- the **common path stays deterministic** (table lookups), so tests and demos are stable;
- an embedding-derived mapping is **recorded, not re-derived**, so the same word means the same thing forever
  for that user until the table is updated;
- a mapping can be **promoted into the synonym table** once a human agrees — the ML shrinks the table's
  maintenance burden instead of replacing it.

## 1.4 Scoring, and how not to match everyone

Today: `score = 3*shared + 2*overlap_hours + 1*both_want_event`.

Proposed: replace `3*shared` with a bounded similarity sum, and **keep the gate that a candidate needs at
least one real overlap** (time still decides whether a card exists at all).

```text
sim(a, b) = 1.0   if same canonical tag
          = 0.5   if same parent domain      (climbing vs hiking)
          = 0.2   if domains are "compatible" (outdoors vs water — both active/outdoor)
          = 0     otherwise

tag_score = Σ over A's tags of max(sim(a, b) for b in B) * idf(a)
```

Using **max per tag, not a full cross-product sum**, keeps the score bounded by `min(|A|, |B|)` and stops a
user with eight tags from inflating every comparison. Then a hard floor: **no card unless at least one pair
reaches ≥ 0.5** — similarities alone never make a card, they only rank pairs that already share time.

Also cap the whole tag component (say 9) so one obsessive shared interest can't outvote the calendar. And keep
"at least one shared *canonical* tag" as a **config toggle** for demos that need conservative behaviour.

## 1.5 Explanations come free, and they matter

Because the taxonomy is explicit, the reason string writes itself:

- "you're both into coffee" (exact)
- "you climb, they hike — both outdoors people" (sibling)
- "you both have Thursday evening free" (time)
- "you boulder, they climb — and both want an event" (mixed)

That string is what each agent reads back to its own student, and it's the difference between "the machine said
so" and a suggestion a person will act on.

## 1.6 Tests worth writing

- `espresso`/`latte`/`cortado` all compare equal to `coffee`.
- `climbing` vs `hiking` yields similarity 0.5 and produces a candidate **only** when time overlaps.
- A pair sharing only `coffee` scores **lower** than a pair sharing only `bouldering` (IDF ordering).
- Eight generic tags on both sides do not outscore two specific shared tags.
- An embedding-mapped tag is recorded with `method="embedding"` and reused deterministically on the next run.
- No similarity, however high, produces a card when their free windows don't intersect.

---

## The second layer you named: hard constraints (and they're not all the same kind)

You described constraints arriving from chat: *"hard constraint is i cannot go over 20 usd."* That is a
different animal from an interest, and it needs three things interests don't: **typed structure, server-side
enforcement, and a privacy class**.

## 2.1 Principles (these are the ones I'd defend)

1. **The agent parses, the server enforces.** the phone agent turns conversation into a *typed* constraint; it never
   sends the sentence. Same allowlist philosophy as everything else: unknown keys are rejected with 422.
2. **A parsed constraint is confirmed back before it becomes binding.** The agent echoes it — *"under $20 per
   person, all week — right?"* — and the constraint carries `confirmed_at`. Same honesty as
   `student_confirmed_at`: provenance, not proof.
3. **Constraints are never shared verbatim with the other side.** Budget and diet are sensitive (they leak
   class, religion, health). The other agent learns only a *boolean feasibility*, never the number.
4. **Hard means hard.** ML may rank; it may never satisfy a constraint. Every violation is a deterministic
   predicate with a reason code.
5. **Constraints are few and visible.** Shown in settings as a list with a delete button; revocable in chat.

## 2.2 Shape

```jsonc
"constraints": [
  {"kind": "budget",   "max_cents": 2000, "currency": "USD", "scope": "per_person", "hardness": "must"},
  {"kind": "time",     "not_after_local": "21:30", "hardness": "must"},
  {"kind": "dietary",  "allow": ["vegan"], "hardness": "must"},
  {"kind": "alcohol",  "allowed": false, "hardness": "must"},
  {"kind": "distance", "max_km": 3, "hardness": "prefer"}
]
```

- `kind` is a closed enum — that enum *is* the feature list.
- `hardness`: `must` filters; `prefer` becomes a negotiation talking point (see Problem 2).
- Storage: `constraints(user_id, kind, value_json, hardness, created_at, confirmed_at, withdrawn_at)`.
  One live row per `(user, kind)` with versions, exactly like grants.
- **Distance is a problem**: it needs a location, and we deliberately collect none. Either add a coarse,
  student-chosen `area` label (campus / north / downtown) — no coordinates — or drop distance constraints for
  now. I would drop them rather than open the location question.

## 2.3 Why typed constraints beat "the agent remembers"

If the constraint lives only in the conversation, three bad things follow: it can't be enforced by the side
that owns the data, it can't be audited, and a different device sees a different world. Typed + stored means
one source of truth, revocable, and testable. It also means the *other* agent can be told "not feasible"
without being told why.

## 2.4 Early unsatisfiability — the cheapest win

Intersect constraints as **intervals** before any search:

```text
budget:   A [0, 2000]  ∩  B [4000, ∞)   → ∅  ⇒ no shared activity is possible, ever
time:     A [.., 21:30] ∩ B [19:00, ..]  → [19:00, 21:30] ✓
dietary:  A {vegan}    ∩  B {vegan, halal} → {vegan} ✓
```

An empty intersection is a **terminal, honest answer**, not something to discover after fifteen rounds:
*"there's nothing under $20 that also works for them — want me to keep your budget, or relax it?"* That single
check saves the entire negotiation loop from the worst failure mode (two agents politely ping-ponging over an
impossible problem).

---

## Problem 2 — the negotiation loop

## 2.5 First, a correction that shapes everything

Your description has two agents "running off" and talking to each other. **They can't.** the phone agent cannot be
called from outside (no webhook, no inbound) — we established this early and it hasn't changed. So the loop is
not live chat between agents; it is a **server-side state machine** that each side *feeds* and *reads* at poll
time:

```text
student ↔ their agent ↔ [typed move] → BROKER → [typed event] → other agent ↔ other student
                                        ↑
                              owns constraints, time, activities,
                              feasibility, versioning, audit
```

The broker is the only place where both students' private constraints exist at once. That's why it must be the
thing that answers feasibility — and why it must answer with booleans, not numbers.

## 2.6 Moves (closed enums, no free text)

| Move | From | Carries |
| --- | --- | --- |
| `propose` | agent | window, optional `activity_id` |
| `counter` | agent | window and/or `activity_id`, plus a `reason` enum: `time` \| `cost` \| `activity` \| `vibe` |
| `accept` | agent | proposal version |
| `decline` | agent | `reason` enum only (never a sentence) |
| `query_activities` | agent → broker | window; **broker answers with filtered options** |
| `feasibility` | broker → agent | `ok` \| `unsat:{budget\|time\|diet}` — no numbers from the other side |

Notice `query_activities`: when you say the agents "run off and search for a rave", what actually happens is
the agent asks the broker, and the broker searches **already filtered by both sides' hard constraints**. The
agent never sees an option the other student couldn't accept, so it can't accidentally propose one.

## 2.7 The loop, bounded

Rules that keep it from becoming an infinite argument:

- **max 3 rounds** per pair (configurable), then a terminal state;
- **progress requirement**: each round must add information — narrow a window, change an activity, or flip a
  `prefer` to `must`. A round that repeats a rejected move is refused with `no_progress`;
- **one open negotiation per pair** (a second is refused) — no state explosion, no two simultaneous plans;
- **wall-clock expiry**: a negotiation dies after 48h, proposal after its own `expires_at`;
- **terminal reasons** (typed): `no_overlap`, `unsat_budget`, `unsat_time`, `no_activity`, `declined`,
  `expired`, `no_progress`. Every terminal maps to one honest sentence for the student.

## 2.8 Your rave example, walked through

> A can do $30. B's hard constraint is $20. The only rave is $30.

```text
1  A's agent: propose {window: Sat 8-11pm, activity: rave-17}
2  BROKER: activity rave-17 costs 3000; B's budget max is 2000
          → does NOT forward it. feasibility = unsat:budget to A's agent
3  A's agent to A: "the only thing on Saturday is $30, which is over their limit.
                    Want me to look for something cheaper, or suggest Friday instead?"
4  A says: "cheaper", and (optionally) relaxes their own preference
5  A's agent: query_activities {window: Sat 8-11pm} → BROKER returns 2 options ≤ $20
6  A's agent: propose {Saturday, option 2}   →  B's agent accepts
```

Three things this buys:

- **B's number never appears** anywhere A can see. A learns "over their limit", which is all A needs.
- The **hopeless move is never made**, so there's no embarrassing rejection to smooth over.
- A's student is given a *choice* (cheaper, or different day) rather than an outcome — the agent's job is to
  widen options, not to conclude for the person.

And the collapse case: if B's budget is $20 and A's is $5, step 2 becomes `unsat:budget` *at intersection time*
(§2.4) and the loop never starts.

## 2.9 What the final suggestion looks like

The outcome is not "match found". It's a **small, explained, acceptable plan**:

```jsonc
{"suggestion": {
  "window": {"display": "Sat 20:00–23:00", "tz": "America/Los_Angeles"},
  "activity": {"title": "Warehouse thing", "price": "$18", "url": "..."},
  "why": ["you both like electronic music",
          "you climb, they hike — same kind of evening",
          "fits both budgets", "fits both calendars"],
  "tradeoff": "you said you'd prefer earlier; this is the only option under $20",
  "decided_by": "both agents, 2 rounds",
  "withdraw": "one tap"
}}
```

`tradeoff` is the honest bit, and it's what makes the agent feel like it was arguing on your behalf rather
than optimising a score.

---

## Where ML belongs, and where it must not

| Decision | Mechanism | Why |
| --- | --- | --- |
| Which canonical tag a word means | table → embedding fallback, recorded | Determinism where it matters; ML shrinks table maintenance |
| Which people are *plausible* | taxonomy similarity + IDF + time | Finite, auditable tables; explainable strings |
| Ranking plausible pairs | deterministic formula | Reproducible demos, no popularity feedback loop |
| Hard constraint satisfaction | **pure predicates** | An ML model must never be able to say "close enough" to a $20 limit |
| Which activity to suggest | broker query, constraint-filtered, then rank by preference | Filter is hard; ranking may be soft |
| What to say to the student | agent, in its own voice | Not the server's job |

The rule I'd write on the wall: **ML proposes candidates; predicates decide admissibility.** The moment a
learned score can override a `must`, the product's central promise ("the system keeps my constraint") is gone.

---

## Risks

| Risk | Mitigation |
| --- | --- |
| Similarity inflation — everyone matches everyone | max-per-tag, IDF, cap, and a ≥0.5 floor; time still gates |
| Taxonomy bias — hand-built domains encode *our* assumptions | keep it small (~40 domains), publish it, allow "compatible" to be asymmetric-free |
| Embedding drift changes who matches | mappings are recorded at ingest, never re-derived |
| Constraint creep — many `must`s → empty market | feasibility check up front, and the agent offers to relax a *preference*, never a `must` |
| Constraints leak class/religion/health | numeric values never leave the broker; only `ok`/`unsat:kind` |
| Infinite negotiation | 3 rounds, progress requirement, typed terminal states |
| Agents mishearing chat ("that's expensive" ≠ $20) | echo-back confirmation before binding; `confirmed_at`; visible in settings |
| Event data (the rave) is scraped/stale | broker-side allowlisted source, `found_at` freshness (already specced in FLOW §9) |

## Phasing (if we build it)

1. **Taxonomy + IDF** in `interests.py`/`matching.py` — no schema change, no new dependency, testable today.
2. **Typed constraints**: payload → schema → table → enforcement at candidate generation. Feasibility
   intersection included, because it's the cheap part.
3. **Activity search** as a broker query, filtered by constraints (§2.8) — still no booking.
4. **Negotiation state machine**: moves, rounds, progress, terminal reasons, audit trail.
5. **Embedding fallback** last, and only if the synonym table shows real gaps.

## Open questions I can't answer alone

1. **Distance**: coarse area label, or no distance constraints at all? (I lean: none, until we decide to hold
   any location data.)
2. **Currency**: single-currency assumption for the demo, or `currency` in every budget?
3. Do we allow **group matches** (>2)? It changes the negotiation from a pair to a small coalition, and the
   constraint intersection gets combinatorial — I'd say no for now.
4. Who authorises **activity sources**? An allowlisted feed is a data-source decision, not an engineering one.
5. Should a `prefer`-violating suggestion ever be shown? (I think yes, with the tradeoff line — a person can
   override their own preference; they can't override someone else's `must`.)
