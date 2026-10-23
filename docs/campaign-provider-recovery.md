# Campaign preparation provider receipt recovery

New semantic dispatches use a background response stream and save its first response ID
before closing the stream and polling for the complete output and usage. Closing the
stream does not cancel a background response. The dispatch hash and original semantic
request stay unchanged; transport streaming does not invalidate accepted paid work.
Every create sends the dispatch ID as metadata, Idempotency-Key and X-Client-Request-Id.
SDK create retries remain disabled.

Independent capability review also retains valid decisions when a response omits or corrupts
an identifier. Only unique, exact, locally valid decisions count. The workflow asks for the
remaining gaps using short request-local selectors and keeps their exact hash bindings in a
separate durable audit. It never fuzzy-matches a returned hash. A focused response that adds no
valid coverage holds preparation for attention instead of repeating paid work or publishing an
incomplete review.

If a retained scoped prose repair contains overlapping replacements or quotes text
that is absent or ambiguous, the workflow asks for one correction per invalid record
and field. It supplies the accepted
original text and every proposed edit for that field, retains the original paid
candidate, and preserves unrelated edits. The consolidated substitutions must still
match unique substrings, stay inside the repair closure and pass normal validation
and independent review. An unmatched quote requires a model to rebase the intended
correction onto the actual baseline; the application never guesses a near match or
moves the edit to another record. A second invalid result in the same field holds
for attention; retry reuses the saved correction receipt instead of submitting it again.

A definitive `incomplete` response with `max_output_tokens` is retained with its exact
output and usage. A prose-only recovery can make a distinct sparse request for the
findings' own chapter/event text fields. Its schema bounds the number of edits to
the available fields and validation permits one edit per record/field pair. The
incomplete full Patch is never accepted, reconstructed or resubmitted. A changed
prompt/schema still adopts the matching original dispatch when its frozen evidence
and image bytes match. Structural changes outside the sparse scope, another exhausted
recovery, invalid edits or unchanged findings remain held; normal independent review
and publication gates still follow any valid correction.

An interruption after the ID is saved resumes by retrieving that same response. A
connection failure before any response receipt remains ambiguous and blocks ordinary
retry. An idempotency header alone is not proof that a repeat create is safe.

For an old ambiguous request, inspect the checkpoint privately:

```sh
python -m tools.campaign_preparation SLUG inspect
```

Use retained logs or the provider's request records to identify the exact response ID.
The documented response retrieval endpoint requires a response ID; the application's
operator tool cannot search remote responses by dispatch metadata. Do not guess IDs,
clear a dispatch row, change the frozen request, or restart the same work as a new paid
submission merely to remove a hold. Preserve the checkpoint and original request time,
dispatch metadata and neighbouring known receipt IDs for reconciliation.

Once the exact response ID is recovered:

```sh
python -m tools.campaign_preparation SLUG reconcile \
  --dispatch-id EXACT_DISPATCH_ID --response-id EXACT_RESPONSE_ID
```

Reconciliation retrieves the response and checks its dispatch metadata; it never creates
a response. Resume separately through the normal preparation retry action. Independent
review, compatibility and publication gates still apply.

Official references, checked 23 October 2026:

- [Background response streaming](https://developers.openai.com/api/docs/guides/background#streaming-a-background-response)
- [Retrieve a response](https://developers.openai.com/api/reference/resources/responses/methods/retrieve)
- [Request ID diagnostics](https://developers.openai.com/api/reference/overview#supplying-your-own-request-id-with-x-client-request-id)
