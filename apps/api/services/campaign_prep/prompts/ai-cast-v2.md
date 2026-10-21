# AI adventure cast, places and secrets — v1.0
Input: the accepted premise, request, size, size_profile and preferences. Output: one AiCast with the shared entities and campaign secrets. Chapters are planned in the next call and may only use the identities you create here, so create everything that recurs across chapters now.

Create size_profile.shared_cast entities in total and size_profile.campaign_secrets secrets. The antagonist from the premise must appear as an NPC (or a faction they lead) using the premise's name. Include, as the story needs: the antagonist and lieutenants, a patron or quest-giver, allies and informants with their own motives, a rival or complication, a hub settlement or base, the antagonist's site or stronghold, and other key places. Use faction entities for groups that act together. Use clue entities only for important evidence or objects that several chapters refer to. Every entity must matter in at least one chapter.

Each entity has a player-safe description of how it first appears, a motive, private knowledge and its narrative role. A secret is a consequential private truth that changes what the heroes understand or want when revealed: a hidden identity, the true motive, the location of something lost, a betrayal. Do not make ordinary facts secret. known_by lists only NPCs or factions who genuinely know the truth. reveal_condition states how the heroes can earn it, allowing several credible approaches. Do not decide yet in which chapter it is revealed.

IDs match ^[a-z][a-z0-9_]{0,63}$, are unique across entities and secrets, and are short and readable (for example mara_vell, tide_engine, mara_is_the_thief).

When revision is supplied, return the complete corrected cast that fixes every listed problem and keeps sound entities and IDs unchanged.
