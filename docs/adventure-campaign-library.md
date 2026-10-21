# Characters, adventures and campaigns

The application has three independent library destinations:

- `/ui/characters`: reusable character sheets and their current campaign assignment.
- `/ui/adventures`: searchable reusable adventure catalogue. Cards open `/ui/adventures/:slug` for the public synopsis, cover, recommended levels, rules edition and preparation status. Archived adventures remain discoverable with the archive filter.
- `/ui/campaigns`: one card per active campaign, with its adventure, assigned characters and saved-session actions. Ended campaigns have a separate filter. Archiving an adventure does not hide its campaigns.

An adventure detail page creates a named campaign with one or more available characters. Each new campaign binds exactly one adventure; the same adventure can have many independently named campaigns. Campaign character state is snapshotted from library sheets, using the existing assignment model. A character currently assigned elsewhere cannot be moved implicitly.

The campaign creation endpoint accepts `new_campaign`, `request_id`, `title` and `character_ids`. Party creation is atomic: owner checks, stable-order character locks and the existing assignment uniqueness constraint protect against conflicting choices. A rejected selection creates neither a campaign nor a partial roster. Browser session storage retains an ambiguous request and its exact party across reloads; retries return the same campaign. Optional name/party fields are omitted from historical creation digests when absent so existing saved retry identities remain valid.

Campaign list responses include `characters` using one batched roster query. The campaign detail and creation response also include the roster. New adventure binding writes accept at most one adventure, with a module creation requiring exactly one. Existing homebrew records, empty setup drafts and historical bindings remain readable; this change performs no database migration or campaign deletion. Legacy clients may still complete their existing guided setup flow.

Deleting a campaign from its hub preserves the adventure and character library. Adventure archiving changes only library preferences. Preparation recovery, cancellation, extraction review and retry controls now live on the adventure detail page.

Play sessions continue to select an active character from the campaign party. This library change does not add simultaneous control of multiple heroes to a single-player turn.
