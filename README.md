# Kanji Grid External Sources Fork

This is a personal fork of [Kanji Grid Kuuube](https://github.com/Kuuuube/kanjigrid), which is itself an improved version of the older Kanji Grid add-on for Anki.

The goal of this fork is narrow: keep the original Kanji Grid behavior, but allow the grid to be enriched with external knowledge sources that are not represented accurately inside Anki.

![Screenshot1](./grid.png)
![Screenshot2](./new_legend.png)
![Screenshot3](./popup.png)



## Warning

This fork is vibe-coded.


For general Kanji Grid usage, read the original project documentation:

- [Kanji Grid Kuuube](https://github.com/Kuuuube/kanjigrid)


## Installation


1. Download the latest release archive from this repository.
2. Open Anki.
3. Go to `Tools` > `Add-ons`.
4. Click `Install from file...`.
5. Select the downloaded add-on archive.
6. Restart Anki.

## Basic Usage

1. Open Anki.
2. Go to `Tools` > `Generate Kanji Grid`.
3. Choose your normal deck and fields as you would in the original add-on.
4. Optionally enable one or more external sources in the setup window.
5. Click `Generate`.

Anki always has priority. If a kanji is already present in the selected Anki cards, the tile keeps the normal Anki-based color. External sources only fill in kanji that are missing from the Anki-derived grid.

## External Sources

### Plain TXT Export

Use this for a simple word list, with one word per line.

Enable:

- `Use external export`
- `Plain TXT export`

Then select the `.txt` file.

These kanji use the same gradient family as Anki, but with a muted/desaturated look because no real review interval is available. The score is based on how many words **From the file** contain the kanji.

### Jiten Backup Export

Use this for a Jiten vocabulary backup JSON.

Enable:

- `Use external export`
- `Jiten backup export`

Then select:

- the Jiten vocabulary backup `.json`
- a JMdict/Yomitan ZIP, such as `JMdict_english.zip`

Jiten backups store word IDs instead of the written words. This fork resolves those IDs through the JMdict/Yomitan ZIP, then uses Jiten's scheduling interval data when available. Jiten-derived kanji use the same gradient family as Anki, but muted/desaturated to show that the data came from outside Anki.

The add-on creates a local cache for the JMdict ID mapping in:

```text
user_files/jmdict_sequence_cache.json
```

### GSM Encounters CSV

Use this for a GSM CSV export of words encountered while playing games.

Enable:

- `Use GSM encounters CSV`

Then select the GSM `.csv` file.

GSM-derived kanji use a separate purple/gray encounter gradient. This is intentionally different from the Anki/Jiten colors because GSM represents exposure frequency, not memory strength or review interval.

## Using Multiple Sources Together

You can use all three layers at the same time:

1. Anki deck and fields
2. Jiten backup export
3. GSM encounters CSV

Priority order:

1. Anki
2. Jiten / TXT external export
3. GSM encounters

That means Anki colors are never overwritten by external sources. Jiten/TXT can fill missing kanji after Anki. GSM can then fill anything still missing.


## Upstream Documentation

This README only documents the fork-specific workflow. For everything else, use the original project:

- Usage and settings: [Kanji Grid Kuuube README](https://github.com/Kuuuube/kanjigrid)
- Grouping data format: [upstream grouping docs](https://github.com/Kuuuube/kanjigrid/blob/master/docs/grouping_data_format.md)
- Original issue tracker: [upstream issues](https://github.com/Kuuuube/kanjigrid/issues)

Please do not report bugs from this fork to the upstream project unless you have reproduced them in the unmodified upstream add-on.
