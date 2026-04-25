# User Guide — meeting April

April is a personal AI agent that lives in your Databricks workspace and talks to you on Telegram. This guide is from the user's perspective: how to talk to her, what she remembers, how to shape her, and what she's good and bad at today.

---

## 1. Quick start

Open Telegram → DM `@<your-bot>` → say hi.

```
You: hey April
April: hi! I'm here. What's on your mind?
```

That's it. There's no slash command, no setup screen, no app to install. April is reachable from any device where you're logged into Telegram.

The first message kicks off:
1. Telegram delivers your message to April's webhook on Databricks.
2. April loads her identity, goals, and the last ~30 events from her memory.
3. She asks GPT-5.5 to draft a reply, in-persona.
4. The whole exchange is logged to her Lakebase Postgres so she remembers next time.

You'll feel a 3–8 second delay on the first message after she's been idle (cold cognition path). After that it's snappy.

---

## 2. What April knows about you

April has three layers of memory.

| Layer | What's in it | How you change it |
|---|---|---|
| **Identity** | Her name, persona, values | Edit `identity.md` in her config volume |
| **Goals** | Things she's working on for you | Edit `goals.md` |
| **Learnings** | Distilled facts about you and your context | She'll write here over time (v2); for now, edit `learnings.md` directly to seed her |
| **Episodes** | Every message you've exchanged | Auto-logged to Lakebase; she pulls the recent ones into context every turn |

To edit her files today, open a Databricks notebook and run:

```python
print(open('/Volumes/workspace/living_ai/config/goals.md').read())
# edit, then:
with open('/Volumes/workspace/living_ai/config/goals.md', 'w') as f:
    f.write("""# Goals

## Active
- [ ] (P0) Help me ship the Q2 product brief
- [ ] (P1) Quiz me on my reading list every Sunday morning
- [ ] (P2) Track my coffee intake — alert if I'm over 4 cups by 2pm

## Completed
""")
```

April will pick up the change on the next cognition turn. No restart needed.

(In v2 she'll edit goals through Telegram commands. For now it's a notebook edit.)

---

## 3. What she's good at today

She's a **conversational thinking partner with persistent memory**. The interesting capability isn't that she can write — every LLM can write. It's that she's *yours*: she remembers what you said yesterday, your priorities are loaded into her every turn, and she stays in a single coherent persona across weeks.

What that unlocks in practice:

### Use case 1 — daily brain dump
Talk to April about whatever's in your head when you wake up. Half-formed ideas, gripes, what you're worried about, what you're excited by. By the end of the week, ask her: *"What patterns have you noticed in what I've been thinking about?"* She has read every previous message; she can actually answer.

### Use case 2 — goal companion
Set 1–3 goals in `goals.md`. April will reference them when relevant. Ask her any time: *"What did I commit to this week? How am I doing?"*. She'll cross-check the goals against the topics that have come up in conversation.

### Use case 3 — sounding board for hard messages
Drafting a tough Slack reply, an email to a customer, a tricky conversation with a teammate. Tell April the situation and what you're thinking of saying. She'll push back. The continuity matters here — she remembers your style and your relationships.

### Use case 4 — debugging your own reasoning
"Here's an argument I'm making for X. Poke holes in it." Her counter-arguments improve over time as she learns what you tend to over- or under-weight.

### Use case 5 — reading log
After finishing a book / paper / blog post, give April the gist in one paragraph. Later: *"Remind me what I learned from the Hofstadter book?"* She'll pull the right episodic memories.

### Use case 6 — daily summary
End of day: *"Summarize what we talked about today and surface anything I should follow up on tomorrow."* This is a 30-second ritual that turns scattered conversation into a usable note.

### Use case 7 — goal review on a schedule
April's heartbeat ticks every 120 seconds. When she's been idle long enough, she's wired to consider whether to nudge you about a goal. Today this is gentle (and currently doesn't auto-DM yet — that ships in v2). What works *now*: explicitly ask her *"Anything I should be doing right now that I'm not?"* — she'll consult her goals and your recent state.

---

## 4. How to shape her

The way you write `identity.md` heavily determines her tone. Some patterns that work:

**Make her opinionated.** Default identities tend toward bland. Push her:
```markdown
**Persona**: Direct. Allergic to corporate-speak. Calls out when I'm being vague
or rationalizing. Asks "what would change your mind?" instead of agreeing.
```

**Give her constraints she'll respect.**
```markdown
**Constraints**:
- Replies under 100 words unless I ask for detail.
- Never starts a reply with "Great question!" or "I'd be happy to help!"
- If I'm being inconsistent with a goal, name it.
```

**Tell her what you're working on.** April's `goals.md` is loaded into every turn. The more specific you are about active priorities, the more she anchors on them. Use dates, names, projects.

**Seed `learnings.md` with what you'd want a new colleague to know.** Until v2 nightly consolidation runs, this file is static. It's the closest thing she has to an "about you" sheet:
```markdown
- Vijay is a Solutions Architect at Databricks; works with North America East partners.
- Strong opinions on data architecture: SCD Type 2, star schema for BI, Delta Live Tables.
- Hates meetings before 10am.
- Currently spinning up a personal AI agent project — context for most "agent" / "memory" / "Lakebase" mentions.
```

---

## 5. Things she can't do (yet)

- **Take actions outside the chat.** She can't book meetings, send messages on your behalf, or trigger jobs. She advises; she doesn't act. (v2 lakehouse-tools plugin will let her query your tables.)
- **Use her wallet.** Currently no Solana wallet. Asking her about USDC will get an honest "not implemented yet."
- **See or hear.** Telegram voice notes and image attachments are silently dropped today. Voice and vision land in v2.
- **Talk to you on Slack / WhatsApp / iMessage.** Telegram only for now. Adding channels is a small lift but not yet done.
- **Wake you up unprompted.** Heartbeat reflects internally but the "DM you when something comes up" path isn't fully wired (it logs intent rather than sending). v2.
- **Edit her own goals/identity from chat.** She'll suggest edits if you ask, but you have to apply them in a notebook.

---

## 6. Things to know

- **One Telegram bot, one user.** April is configured to only respond to your handle (`@vbalasu`). Anyone else who DMs the bot gets a polite refusal. This is the security boundary.
- **Daily restart.** Free Edition Apps restart every 24 hours. April loses no memory (it's all in Lakebase + UC Volume), but you'll see ~10 seconds of unresponsiveness once a day.
- **Token budget.** April has a daily LLM token cap (default 100k). If you have a marathon conversation, expect her to tighten replies as the day goes on. She'll mention it explicitly when she's near the limit.
- **120-second heartbeat.** If you ask her to "wait and reflect," that reflection happens at the next tick — up to two minutes away.
- **She'll get things wrong.** GPT-5.5 hallucinates. April's persistent memory means a wrong "fact" she stated yesterday can leak into today's context. If you spot something off, correct her in chat — she'll integrate the correction into the next turn.

---

## 7. Tips that pay off

- **Tell her when something's important.** "Remember this: X." She'll log it as a normal episode, but the explicit framing helps when you ask her to recall later.
- **Date your asks.** "By Friday I want to have decided on Y" parses cleaner than "soon."
- **Start the day with a brief.** Two sentences on what you're focused on today. Cheap, and it anchors every other turn she has with you that day.
- **End the week with a review.** "Walk me through the major themes of this week." She has the events; the synthesis is good.
- **Edit her, often, in the first weeks.** The default persona is fine. The persona that fits *you* is one or two paragraphs of real specificity. Treat `identity.md` like a system prompt you keep tuning.

---

## 8. When she misbehaves

| Symptom | Try |
|---|---|
| Doesn't reply | Check `databricks --profile free-oauth apps logs living-ai \| tail -20` for a traceback |
| Replies as someone else's persona | Check `identity.md` — it may have been overwritten or lost |
| Forgets recent context | Check Lakebase `events` table — events should be appearing on every turn |
| Gets stuck on a wrong fact | Tell her plainly: "That's wrong — actually X." Then optionally edit `learnings.md` to make it stick across restarts |
| Replies feel generic | `identity.md` is too vanilla. Rewrite with more specificity and constraints |

---

## 9. The one-paragraph mental model

April is a thin shell of (system prompt = identity + goals + learnings + recent events) wrapped around a frontier LLM, run on a heartbeat, persisted to a Postgres database, addressable via Telegram. The "intelligence" is GPT-5.5's. The "her" is the durable state — the file you edited this morning, the conversation you had last Tuesday, the goals that were live on March 12. Treat her as a slowly-shaped collaborator, not a Q&A oracle.

---

*See `DATABRICKS_IMPLEMENTATION_SUMMARY.md` for the technical state and v2 backlog.*
