import type { AdventureJobRead } from "../api/types";

export function isJobActive(job?: AdventureJobRead | null): boolean {
  return !!job && ["queued", "running", "cancel_requested"].includes(job.state);
}

export function preparationProgress(job?: AdventureJobRead | null): string | null {
  if (!job) return null;
  if (job.state === "cancel_requested") return "Cancellation requested; finishing at the next safe checkpoint.";
  if (job.state === "queued") return "Preparation is queued.";
  if (job.stage === "outline") return "Designing the campaign outline.";
  if (job.stage === "generating_chapters") {
    return `Generating campaign chapters — ${job.completed_chapters} of ${job.total_chapters ?? "the required"} saved.`;
  }
  if (job.stage === "extracting") return "Reading and checking the uploaded source.";
  if (job.stage === "embedding") return "Building the searchable campaign index.";
  if (job.stage === "indexing") return "Preparing the validated campaign index.";
  if (job.stage === "publishing") return "Publishing the validated campaign source.";
  return "Preparation is in progress.";
}

export function jobFailureHeading(job: AdventureJobRead): string {
  if (job.state === "interrupted") return "Preparation stopped after an interruption.";
  if (job.state === "cancelled") return "Preparation was cancelled.";
  if (job.stage === "outline") return "Preparation failed while designing the outline.";
  if (job.stage === "generating_chapters") return "Preparation failed while generating chapters.";
  if (job.stage === "extracting") return "Preparation failed while reading the source.";
  if (job.stage === "embedding" || job.stage === "indexing") return "Preparation failed while building the index.";
  if (job.stage === "publishing") return "Preparation failed while publishing the validated source.";
  return "The latest preparation attempt failed.";
}
