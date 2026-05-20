# Changelog

## Unreleased

- Added external source support for plain TXT word lists.
- Added Jiten backup JSON support with JMdict/Yomitan ZIP word-ID resolution.
- Added Jiten API support with a persisted password-style token field.
- Added GSM local API support and GSM encounters CSV fallback.
- Added mixed Anki + Jiten/TXT + GSM grid generation with Anki priority.
- Added muted external-source colors and a GSM-specific encounter legend.
- Reworked the external-source setup UI so irrelevant file fields are hidden when API mode is active.
- Added saved setup selection for deck, fields, grouping, and language.
- Added per-group filtered study deck creation from generated grids.
- Added `Study now` actions that open the appropriate filtered deck directly.
- Added optional temporary study deck cleanup.
- Added manual study deck refresh from the `Data` tab.
- Added startup refresh for existing Kanji Grid study decks.
- Added note-add refresh for existing Kanji Grid study decks, intended for Yomitan mining workflows.
- Added a lightweight collection watcher for Yomitan/AnkiConnect note additions that bypass Anki's add-card UI hooks.
- Changed automatic study-deck updates to append newly mined cards incrementally instead of rebuilding all study decks.
- Added a separate unseen-card count label beside group study actions.
- Refreshed Anki's deck list after creating a group study deck.
