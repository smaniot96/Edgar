"""AI-authored campaigns.

The model designs a full adventure — premise, factions, NPCs, and a chapter outline — then
expands each chapter into rich module text. The result is embedded through the same pipeline as
an uploaded PDF (`ingestion.ingest_pages`), so a generated campaign is playable identically:
reviewed scene retrieval, opening narration, and combat use the same source contract. Generated manuscripts require explicit manifest review before module play.

Size controls scope: more chapters + richer guidance => a considerably longer campaign.
Generation runs as a background task; status is tracked in the adventure metadata
(processing -> ready | failed), exactly like uploads.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.llm import make_chat_model, message_text

log = structlog.get_logger()

# Chapters per size tier — the main lever on length. Concurrency is bounded so a "gigantic"
# run doesn't open 24 OpenAI calls at once.
SIZE_CHAPTERS: dict[str, int] = {"small": 3, "medium": 6, "large": 12, "gigantic": 24}
_MAX_CONCURRENT = 6


class IncompleteGeneration(ValueError):
    """The requested manuscript is incomplete; retry without indexing partial text."""


_SIZE_GUIDANCE: dict[str, str] = {
    "small": "a tight one-shot: a single arc and location, one clear antagonist, 1-2 hours of play.",
    "medium": "a short adventure: a few connected scenes, a subplot or two, several NPCs, one boss.",
    "large": "a multi-act adventure: several distinct locations, rival factions, subplots, multiple "
    "encounters, secrets, and an escalating climax.",
    "gigantic": "an epic, multi-chapter saga: many interlocking factions and NPCs, branching threads, "
    "several mini-bosses building to a final confrontation, evolving stakes across acts, and rich lore.",
}


class ChapterOutline(BaseModel):
    name: str
    summary: str = Field(
        description="2-3 sentences: what happens, the location, and the goal of this chapter."
    )


class CampaignOutline(BaseModel):
    title: str
    premise: str = Field(
        description="2-4 sentences setting up the whole adventure and its central conflict."
    )
    setting: str = Field(description="Where it takes place; tone and atmosphere.")
    hook: str = Field(description="How the player characters get drawn in at the very start.")
    factions: list[str] = Field(default_factory=list)
    key_npcs: list[str] = Field(default_factory=list, description="'Name — role/motivation' lines.")
    chapters: list[ChapterOutline] = Field(default_factory=list)


def _outline_system(size: str, n_chapters: int, rules_edition: str = "2014") -> str:
    return (
        f"You are a master Dungeon Master and adventure designer for D&D 5e, {rules_edition} edition. Design "
        f"{_SIZE_GUIDANCE.get(size, _SIZE_GUIDANCE['medium'])} "
        f"Produce a coherent outline with EXACTLY {n_chapters} chapters that build on each other "
        "with rising tension and a satisfying climax and resolution. Make it original, vivid, and "
        "genuinely fun for human players, with real choices, memorable NPCs, and clear stakes. "
        "Chapter 1 must establish where the characters begin and the hook that starts the story."
    )


def _chapter_system(rules_edition: str = "2014") -> str:
    return (
        f"You are writing one chapter of a D&D 5e {rules_edition} edition adventure module for the Dungeon Master to run. "
        "Write rich, runnable markdown for THIS chapter only: a short read-aloud/boxed intro, the "
        "key locations and what's there, the NPCs (with motivations and a line or two of dialogue), "
        "encounters (with rough monster types and difficulty for a low-level party), skill "
        "challenges, secrets/clues, treasure, and how it connects to the next chapter. Keep it "
        "consistent with the overall outline. Use evocative but concise prose. Do not address the "
        "AI or the player directly; write it as module text."
    )


async def _generate_outline(
    title_hint: str, theme: str, size: str, n_chapters: int, rules_edition: str = "2014"
) -> CampaignOutline:
    llm = make_chat_model(temperature=0.8)
    structured = llm.with_structured_output(CampaignOutline, method="function_calling")
    asks = []
    if title_hint:
        asks.append(f"Preferred title: {title_hint}.")
    if theme:
        asks.append(f"Theme / premise the player asked for: {theme}.")
    asks.append("Design the adventure now.")
    outline = await structured.ainvoke(
        [
            SystemMessage(content=_outline_system(size, n_chapters, rules_edition)),
            HumanMessage(content=" ".join(asks)),
        ]
    )
    return outline


async def _expand_chapter(
    outline: CampaignOutline,
    index: int,
    total: int,
    sem: asyncio.Semaphore,
    rules_edition: str = "2014",
) -> str:
    chapter = outline.chapters[index]
    is_first = index == 0
    is_last = index == total - 1
    role = (
        "This is the OPENING chapter: begin the story here, establish the starting location and the "
        "hook, and draw the players in."
        if is_first
        else "This is the FINAL chapter: deliver the climax and a satisfying resolution."
        if is_last
        else "This is a middle chapter: advance the plot and raise the stakes."
    )
    context = (
        f"Adventure title: {outline.title}\n"
        f"Premise: {outline.premise}\n"
        f"Setting: {outline.setting}\n"
        f"Hook: {outline.hook}\n"
        f"Factions: {', '.join(outline.factions) or 'n/a'}\n"
        f"Key NPCs: {'; '.join(outline.key_npcs) or 'n/a'}\n"
        f"Full chapter list: {', '.join(c.name for c in outline.chapters)}\n\n"
        f'Write Chapter {index + 1} of {total}: "{chapter.name}".\n'
        f"Chapter summary: {chapter.summary}\n{role}"
    )
    async with sem:
        llm = make_chat_model(temperature=0.8)
        resp = await llm.ainvoke(
            [SystemMessage(content=_chapter_system(rules_edition)), HumanMessage(content=context)]
        )
    body = message_text(resp)
    if len(re.findall(r"\b\w+\b", body)) < 20:
        raise IncompleteGeneration(
            f"Chapter {index + 1} produced too little manuscript text. Retry generation."
        )
    return f"## Chapter {index + 1}: {chapter.name}\n\n{body.strip()}"


async def generate_campaign(
    title_hint: str,
    theme: str,
    size: str,
    rules_edition: str = "2014",
    *,
    checkpoint: dict[str, Any] | None = None,
    on_checkpoint: Callable[[dict[str, Any], str], None] | None = None,
) -> tuple[str, str, list[tuple[int, str]]]:
    """Returns (title, description, pages) where pages are `(page_no, markdown)` for embedding.

    Page 1 carries the title/premise/hook + opening chapter so the opening-scene intro (which
    reads the earliest pages) starts the player at the true beginning.
    """
    n_chapters = SIZE_CHAPTERS.get(size, SIZE_CHAPTERS["medium"])
    saved = checkpoint or {}
    saved_outline = saved.get("outline")
    outline = (
        CampaignOutline.model_validate(saved_outline)
        if isinstance(saved_outline, dict)
        else await _generate_outline(title_hint, theme, size, n_chapters, rules_edition)
    )
    if len(outline.chapters) != n_chapters:
        raise IncompleteGeneration(
            f"The generated outline contains {len(outline.chapters)} of {n_chapters} required chapters. Retry generation."
        )
    total = len(outline.chapters) or 1

    chapters = dict(saved.get("chapters") or {})
    if any(not key.isdigit() or int(key) >= total for key in chapters):
        raise ValueError("Saved chapter checkpoint does not match the requested campaign size.")
    sem = asyncio.Semaphore(_MAX_CONCURRENT)
    if on_checkpoint and saved_outline is None:
        on_checkpoint(
            {"version": 1, "outline": outline.model_dump(), "chapters": chapters},
            "generating_chapters",
        )
    tasks = {
        asyncio.create_task(_expand_chapter(outline, i, total, sem, rules_edition)): i
        for i in range(total)
        if str(i) not in chapters
    }
    pending = set(tasks)
    failure: BaseException | None = None
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                index = tasks[task]
                try:
                    chapters[str(index)] = task.result()
                except BaseException as exc:
                    failure = failure or exc
            if on_checkpoint and done and failure is None:
                try:
                    on_checkpoint(
                        {"version": 1, "outline": outline.model_dump(), "chapters": chapters},
                        "generating_chapters",
                    )
                except BaseException as exc:
                    failure = failure or exc
            if failure is not None:
                break
    except BaseException as exc:
        # External cancellation and callback/provider errors must not leave paid provider
        # tasks running after this attempt has returned to the request handler.
        failure = failure or exc
    finally:
        pending_tasks = [task for task in tasks if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            settled = await asyncio.gather(*pending_tasks, return_exceptions=True)
            # Keep the task/outcome association deterministic. A set's iteration order
            # must never decide which chapter receives another task's successful result.
            for task, outcome in zip(pending_tasks, settled, strict=True):
                if isinstance(outcome, str):
                    chapters[str(tasks[task])] = outcome
        for task, index in tasks.items():
            if task.done() and not task.cancelled():
                try:
                    result = task.result()
                except BaseException:
                    continue
                if isinstance(result, str):
                    chapters[str(index)] = result
        if on_checkpoint and tasks:
            try:
                on_checkpoint(
                    {"version": 1, "outline": outline.model_dump(), "chapters": chapters},
                    "generating_chapters",
                )
            except BaseException as exc:
                failure = failure or exc
    if failure is not None:
        raise failure
    chapter_texts = [chapters[str(i)] for i in range(total)]

    front_matter = (
        f"# {outline.title}\n\n"
        f"{outline.premise}\n\n"
        f"**Setting.** {outline.setting}\n\n"
        f"**How it begins.** {outline.hook}\n\n"
        + (f"**Factions.** {', '.join(outline.factions)}\n\n" if outline.factions else "")
        + (f"**Key NPCs.** {'; '.join(outline.key_npcs)}\n\n" if outline.key_npcs else "")
    )

    pages: list[tuple[int, str]] = []
    if chapter_texts:
        pages.append((1, front_matter + chapter_texts[0]))
        for i, text in enumerate(chapter_texts[1:], start=2):
            pages.append((i, text))
    else:
        pages.append((1, front_matter))

    return outline.title, outline.premise, pages


async def build_generated_campaign(
    slug: str, title_hint: str, theme: str, size: str, *, attempt_id: str | None = None
) -> None:
    """Background worker: generate the campaign, embed it, flip metadata to ready/failed."""
    from db.vector.versions import SupersededIngestion
    from ingestion.embed import ingest_pages
    from ingestion.readiness import IngestionRejected, report_summary

    from . import adventures_meta as store
    from .adventures_meta import load_meta

    token = attempt_id or store.begin_attempt(slug, job_attempt=True, job_kind="generation")
    job = store.ensure_job(slug, token, kind="generation", stage="outline")
    job_id = job["job_id"]
    report = {}
    receipt_updates = {}

    try:
        checkpoint = store.load_generation_checkpoint(
            slug, job_id, expected_chapters=SIZE_CHAPTERS.get(size, SIZE_CHAPTERS["medium"])
        )
    except (OSError, ValueError) as exc:
        store.update_job(
            slug,
            token,
            state="failed",
            stage="failed",
            retryable=False,
            checkpoint_corrupt=True,
        )
        store.finish_attempt(
            slug,
            token,
            {
                "status": "failed",
                "failure_code": "checkpoint_unreadable",
                "error": "Saved generation progress is unreadable. Create a new campaign to generate again.",
            },
        )
        log.warning("campaign_checkpoint_unreadable", slug=slug, error=type(exc).__name__)
        return

    def checkpointed(value: dict[str, Any], stage: str) -> None:
        from .adventures_meta import save_generation_checkpoint

        save_generation_checkpoint(
            slug,
            token,
            {"version": 1, "job_id": job_id, **value},
            stage=stage,
            total_chapters=SIZE_CHAPTERS.get(size, SIZE_CHAPTERS["medium"]),
        )

    def prepared(value: dict) -> None:
        report.update(value)
        store.prepare_attempt(slug, token, value, receipt_updates)

    structlog.contextvars.bind_contextvars(adventure_slug=slug)
    try:
        store.check_job_running(slug, token)
        title, description, pages = await generate_campaign(
            title_hint,
            theme,
            size,
            load_meta(slug).get("rules_edition", "2014"),
            checkpoint=checkpoint,
            on_checkpoint=checkpointed,
        )
        store.update_job(slug, token, stage="indexing", retained_work="complete_manuscript")
        from .manuscript import save_candidate_manuscript

        manuscript = "\n\n".join(text for _, text in sorted(pages))
        save_candidate_manuscript(slug, token, manuscript)
        receipt_updates.update(title=title, description=description)
        publication_attempt = 0

        def publication_guard():
            nonlocal publication_attempt
            publication_attempt += 1
            return store.publication_guard(
                slug,
                token,
                stage="embedding" if publication_attempt == 1 else "publishing",
            )

        n = await asyncio.to_thread(
            ingest_pages,
            pages,
            slug,
            slug,
            expected_pages=SIZE_CHAPTERS.get(size, SIZE_CHAPTERS["medium"]),
            rules_edition=load_meta(slug).get("rules_edition", "unspecified"),
            preview_dir=store.attempt_dir(slug, token),
            on_prepared=prepared,
            replace=True,
            publication_guard=publication_guard,
        )
        meta = load_meta(slug)
        from .campaign_sources import inspect_ingestion_source

        coverage = await asyncio.to_thread(
            inspect_ingestion_source,
            slug,
            {**meta, "chunks": n, "ingestion": report_summary(report)},
        )
        ready = n > 0 and report.get("quality_passed") and coverage["status"] == "ready"
        store.finish_attempt(
            slug,
            token,
            {
                "title": title or meta.get("title"),
                "description": description,
                "status": "ready" if ready else "failed",
                "chunks": n,
                "failure_code": None if ready else "index_coverage",
                "error": None
                if ready
                else "The generated index does not match its validated text. Inspect the preview and retry generation.",
            },
            success=bool(ready),
        )
        log.info("campaign_generated", slug=slug, chunks=n, size=size)
    except store.JobCancelled:
        store.cancel_attempt(slug, token)
        log.info("campaign_generation_cancelled", slug=slug)
    except SupersededIngestion:
        log.info("campaign_generation_superseded", slug=slug)
    except IngestionRejected as exc:
        store.finish_attempt(
            slug,
            token,
            {
                "status": "failed",
                "chunks": 0,
                "failure_code": exc.code,
                "error": str(exc),
                "ingestion": report_summary(exc.report),
            },
        )
        log.warning("campaign_quality_rejected", slug=slug, code=exc.code)
    except IncompleteGeneration as exc:
        store.finish_attempt(
            slug,
            token,
            {
                "status": "failed",
                "chunks": 0,
                "failure_code": "generation_incomplete",
                "error": str(exc),
            },
        )
        log.warning("campaign_generation_incomplete", slug=slug)
    except Exception as exc:
        log.exception("campaign_generation_failed", slug=slug)
        # Raw exception text can carry provider internals; the full trace is in the logs.
        store.finish_attempt(
            slug,
            token,
            {
                "status": "failed",
                "failure_code": "generation_error",
                "error": f"Generation failed ({type(exc).__name__}); retry generation or see server logs.",
            },
        )
    finally:
        structlog.contextvars.unbind_contextvars("adventure_slug")
