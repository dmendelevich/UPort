---
name: uport-screen-standardization
description: Use this skill whenever working on UPort's Telegram bot screens — standardizing, refactoring, redesigning, or fixing a card/screen/menu (portfolio card, strategy card, ticker card, summary screen, any bot_handlers/*.py view), or when the user says things like "стандартизируем экран X", "разберём карточку Y", "давай приведём в порядок кубики", or wants a UI component shared across screens. Make sure to trigger this even if the user doesn't say "standardize" explicitly — any request to change how a screen looks, fix a display bug, add a field to a card, or make two screens consistent should go through this procedure rather than being patched inline. This encodes the exact discuss-then-code-then-test rhythm the UPort project owner has established over many sessions — skipping it produces rework and breaks trust.
---

# UPort screen/"kubik" standardization procedure

UPort's Telegram screens (portfolio card, strategy card, ticker card, summary, etc.) are built from shared functions in `bot_handlers/bot_screens.py` (text blocks) and `bot_handlers/bot_keyboards.py` (keyboards) — see the project's root `CLAUDE.md` for the standing technical rules (screen width, header/body/footer order, currency, time). This skill is the *procedure* for actually doing standardization work: the sequence that has repeatedly caught real bugs and avoided rework, versus the tempting shortcut of "just edit the one screen the user is looking at."

The core reason this procedure exists: the same visual "kubik" (block) almost always turns out to be rendered independently in more than one place, with subtly different bugs in each copy. Fixing only the screen currently in view leaves the other copies broken, and the user *will* find them later. Every step below exists to prevent a specific failure mode that has actually happened on this project — treat them as load-bearing, not bureaucratic.

## 0. Whose topic is this?

The user drives the topic and its scope — don't propose new standardization targets unprompted, and don't silently expand scope mid-task (e.g., don't wander from "fix the portfolio card" into "also rewrite the ticker card" without asking). If you spot a related issue while working, note it and ask, or add it to `Claude/BACKLOG.md` for later — don't just do it.

## 1. Find ALL live variants — before anything else

Grep the *entire* codebase for the screen/feature in question, not just the file the user pointed at. Do not rely on memory of what you found last time (memory notes, `Claude/*.md`, and even your own earlier read of the file can be stale — the code is the source of truth). Look specifically for:

- Every place that renders similar content via a different code path (e.g., a "family view" mode, a "research portfolio" mode, a global-search variant — these accumulate because features get added incrementally to whichever screen was open at the time).
- Every place that calls the block/formatter you're about to touch, so you know the blast radius of a change.
- Dead code that *looks* like a live variant but is actually unreachable (e.g., after a screen is removed, a mode that only that screen could trigger becomes dead — note it, don't assume it still matters, but don't silently delete it either unless asked).

Use `grep -rn` across `bot_handlers/`, `analytics/`, and anywhere else relevant. If you're not sure the search was thorough enough, it wasn't — widen it.

## 2. Split common vs. context-specific

For each variant found in step 1, work out what's genuinely the *same concept* rendered differently (a candidate for one shared function) versus what's *actually* different content that happens to look similar. Getting this split wrong in either direction causes real damage:
- Merging things that are actually different (e.g., a portfolio's own trade-account cash vs. a family-wide aggregate) produces a function with a pile of `if` branches for cases that don't belong together.
- Failing to merge things that are actually the same concept (e.g., a risk-audit block duplicated with slightly different wording in two screens) is exactly the "same kubik, N independent buggy copies" problem this whole procedure exists to avoid.

## 3. Discuss principles and edge cases — no code yet

Lay out the header/body/footer breakdown, the common-vs-specific split, and any edge cases (what happens for a portfolio with zero holdings? a synthetic/aggregate portfolio? a currency the code hasn't handled before?) for the user explicitly. Don't resolve edge cases unilaterally — they carry real domain knowledge (which fields are legacy and being phased out, which behaviors were deliberate past decisions) that isn't visible from the code alone. Wait for explicit agreement that the approach is settled before writing any code. A quick one-line confirmation ("Ok, write the code") is enough once the design itself has actually been discussed — the point isn't ceremony, it's making sure you're not building the wrong thing.

## 4. Decompose by meaning, not by screen

The single most important lesson from past sessions: don't organize the work as "screen A's content" vs "screen B's content." Organize it as blocks of meaning (a "capital plan vs. actual" block, a "risk audit" block, a "position list" block) — the same block legitimately shows up on more than one screen (a ticker's owner card and its "also held here" section; a strategy's risk audit and a portfolio's roll-up of every strategy's risk audit). Classifying by screen first is a trap that leads to duplicated, drifting implementations. If asked "does this belong to screen A or screen B," the better question is usually "what block is this, and which screens need it."

## 5. One shared function per block/entity type

Write the shared builder in `bot_handlers/bot_screens.py` for text blocks, `bot_handlers/bot_keyboards.py` for keyboard rows. The granularity that has worked well: one function per *entity type* (a strategy's header, a portfolio's risk-audit rollup) — not one giant function for an entire screen, and not a micro-function per tiny fragment either. A good sign you've got the grain right: the function is independently useful to a screen you haven't touched yet.

Money and currency: display amounts in the *viewer's* currency (`state: FSMContext` → `user_db_id` → `public.users.base_currency`), computing the FX rate once per render and passing it into the block function as a parameter — never have a block function query the database for its own exchange rate. If a single render legitimately mixes several *different* source currencies (e.g., a portfolio's assets each have their own native currency), cache the rate per source-currency code within that render instead of assuming one global rate — check whether that's actually the situation before assuming it isn't.

## 6. Migrate one call site at a time

Once the shared function exists, switch over the call sites one at a time, testing after each — not a single sweeping find-and-replace across every screen. This is slower per-step but makes it obvious which specific change caused a regression if one shows up, and lets the user sanity-check one piece at a time instead of a big unreviewable diff.

## 7. Test in layers — the terminal lies about visual/width issues

Each of these layers catches a different failure mode; skipping to the last one (or skipping testing entirely because "it compiles") is how visual bugs and silent regressions slip through:

1. **Direct function call against real data.** Run the new/changed function directly (via `venv`) against real rows from the actual database — not synthetic fixtures. Real data surfaces real edge cases (NULLs, zero-holdings positions, unusual currencies) that a hand-picked test case won't.
2. **Full mock-handler run.** Drive the actual `@router.callback_query` handler with `unittest.mock` (`AsyncMock`/`MagicMock` for the callback and FSM state) and a real `MenuAction`, hitting the real database. This exercises the whole path — routing, state, keyboard assembly — not just the isolated function.
3. **A real Telegram message.** Send an actual test message via `aiogram.Bot` (using `TELEGRAM_TOKEN` from `.env`) to a real chat. This step exists specifically because Telegram's font is proportional, not monospace — a terminal print can look perfectly aligned while the same text renders ragged on an actual phone. Any change touching character widths, alignment, or button layout needs this step; don't skip it and assume the terminal output is representative.

## 8. Restart and get a live check from the user

After finishing a coherent chunk of work (a block, or a whole topic), run `restart_uport.sh` and ask the user to verify live in Telegram before considering it done. Real bugs have repeatedly surfaced only at this step, not in any of the earlier test layers — treat a live check as part of testing, not an optional formality after testing is "done."

## 9. Log decisions as you go

Write what was decided and why into `Claude/BACKLOG.md` (which decisions, which trade-offs, what was explicitly deferred and why) as each piece lands — not saved up to reconstruct from memory at the end of a long session. Follow the file's existing conventions: a running "Сделано" log entry per completed piece of work, "Открыто" items per track for anything deferred, and note explicitly when something was *considered and rejected* (that's just as valuable to future-you as what was built, since it prevents re-litigating a settled question).

## Git workflow (already in `CLAUDE.md`, repeated here because it bites)

Commit to `master` directly (no PRs) after a topic wraps up. Always ask before `git push`, separately, every single time — even if "commit after the topic" is already the standing agreement. This is deliberate: it keeps a push from happening "by inertia" alongside something else the user agreed to earlier, in a different context.
