"""AI-authored campaigns.

The model designs a full adventure — premise, factions, NPCs, and a chapter outline — then
expands each chapter into rich module text. The result is embedded through the same pipeline as
an uploaded PDF (`ingestion.ingest_pages`), so a generated campaign is playable identically:
the opening-scene intro, RAG grounding, and combat all work with no special-casing.

Size controls scope: more chapters + richer guidance => a considerably longer campaign.
Generation runs as a background task; status is tracked in the adventure metadata
(processing -> ready | failed), exactly like uploads.
"""

from __future__ import annotations

import asyncio
import re

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


def _outline_system(size: str, n_chapters: int) -> str:
    return (
        "You are a master Dungeon Master and adventure designer for D&D 5e. Design "
        f"{_SIZE_GUIDANCE.get(size, _SIZE_GUIDANCE['medium'])} "
        f"Produce a coherent outline with EXACTLY {n_chapters} chapters that build on each other "
        "with rising tension and a satisfying climax and resolution. Make it original, vivid, and "
        "genuinely fun for human players, with real choices, memorable NPCs, and clear stakes. "
        "Chapter 1 must establish where the characters begin and the hook that starts the story."
    )


def _chapter_system() -> str:
    return (
        "You are writing one chapter of a D&D 5e adventure module for the Dungeon Master to run. "
        "Write rich, runnable markdown for THIS chapter only: a short read-aloud/boxed intro, the "
        "key locations and what's there, the NPCs (with motivations and a line or two of dialogue), "
        "encounters (with rough monster types and difficulty for a low-level party), skill "
        "challenges, secrets/clues, treasure, and how it connects to the next chapter. Keep it "
        "consistent with the overall outline. Use evocative but concise prose. Do not address the "
        "AI or the player directly; write it as module text."
    )


async def _generate_outline(
    title_hint: str, theme: str, size: str, n_chapters: int
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
            SystemMessage(content=_outline_system(size, n_chapters)),
            HumanMessage(content=" ".join(asks)),
        ]
    )
    return outline


async def _expand_chapter(
    outline: CampaignOutline, index: int, total: int, sem: asyncio.Semaphore
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
            [SystemMessage(content=_chapter_system()), HumanMessage(content=context)]
        )
    body = message_text(resp)
    if len(re.findall(r"\b\w+\b", body)) < 20:
        raise IncompleteGeneration(
            f"Chapter {index + 1} produced too little manuscript text. Retry generation."
        )
    return f"## Chapter {index + 1}: {chapter.name}\n\n{body.strip()}"


async def generate_campaign(
    title_hint: str, theme: str, size: str
) -> tuple[str, str, list[tuple[int, str]]]:
    """Returns (title, description, pages) where pages are `(page_no, markdown)` for embedding.

    Page 1 carries the title/premise/hook + opening chapter so the opening-scene intro (which
    reads the earliest pages) starts the player at the true beginning.
    """
    n_chapters = SIZE_CHAPTERS.get(size, SIZE_CHAPTERS["medium"])
    outline = await _generate_outline(title_hint, theme, size, n_chapters)
    if len(outline.chapters) != n_chapters:
        raise IncompleteGeneration(
            f"The generated outline contains {len(outline.chapters)} of {n_chapters} required chapters. Retry generation."
        )
    total = len(outline.chapters) or 1

    sem = asyncio.Semaphore(_MAX_CONCURRENT)
    chapter_texts = await asyncio.gather(
        *[_expand_chapter(outline, i, total, sem) for i in range(len(outline.chapters))]
    )

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


async def build_generated_campaign(slug: str, title_hint: str, theme: str, size: str) -> None:
    """Background worker: generate the campaign, embed it, flip metadata to ready/failed."""
    from ingestion.embed import ingest_pages
    from ingestion.readiness import IngestionRejected, report_summary

    from .adventures_meta import ADVENTURES_DIR, load_meta, save_meta

    def prepared(report: dict) -> None:
        meta = load_meta(slug)
        meta.update({"ingestion": report_summary(report), "failure_code": None})
        save_meta(slug, meta)

    structlog.contextvars.bind_contextvars(adventure_slug=slug)
    try:
        title, description, pages = await generate_campaign(title_hint, theme, size)
        n = await asyncio.to_thread(
            ingest_pages,
            pages,
            slug,
            slug,
            expected_pages=SIZE_CHAPTERS.get(size, SIZE_CHAPTERS["medium"]),
            preview_dir=ADVENTURES_DIR / slug,
            on_prepared=prepared,
            replace=True,
        )
        meta = load_meta(slug)
        from .campaign_sources import inspect_ingestion_source

        report = meta.get("ingestion") or {}
        coverage = await asyncio.to_thread(inspect_ingestion_source, slug, {**meta, "chunks": n})
        ready = n > 0 and report.get("quality_passed") and coverage["status"] == "ready"
        meta.update(
            {
                "title": title or meta.get("title"),
                "description": description,
                "status": "ready" if ready else "failed",
                "chunks": n,
                "failure_code": None if ready else "index_coverage",
                "error": None
                if ready
                else "The generated index does not match its validated text. Inspect the preview and retry generation.",
            }
        )
        save_meta(slug, meta)
        log.info("campaign_generated", slug=slug, chunks=n, size=size)
    except IngestionRejected as exc:
        meta = load_meta(slug)
        meta.update(
            {
                "status": "failed",
                "chunks": 0,
                "failure_code": exc.code,
                "error": str(exc),
                "ingestion": report_summary(exc.report),
            }
        )
        save_meta(slug, meta)
        log.warning("campaign_quality_rejected", slug=slug, code=exc.code)
    except IncompleteGeneration as exc:
        meta = load_meta(slug)
        meta.update(
            {
                "status": "failed",
                "chunks": 0,
                "failure_code": "generation_incomplete",
                "error": str(exc),
            }
        )
        save_meta(slug, meta)
        log.warning("campaign_generation_incomplete", slug=slug)
    except Exception as exc:
        log.exception("campaign_generation_failed", slug=slug)
        meta = load_meta(slug)
        # Raw exception text can carry provider internals; the full trace is in the logs.
        meta.update(
            {
                "status": "failed",
                "failure_code": "generation_error",
                "error": f"Generation failed ({type(exc).__name__}); retry generation or see server logs.",
            }
        )
        save_meta(slug, meta)
    finally:
        structlog.contextvars.unbind_contextvars("adventure_slug")
