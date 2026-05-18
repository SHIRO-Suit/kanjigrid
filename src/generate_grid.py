import datetime
import csv
import json
import math
import operator
import os
import re
import sys
import types
import urllib.parse
import zipfile
from functools import reduce

from anki.utils import ids2str

from . import data, util


JITEN_INTERVAL_MARKER = 1000000
GSM_ENCOUNTER_MARKER = 2000000
EXTERNAL_SCORE_SCALE = 1000


def valid_unit_key(config: types.SimpleNamespace, unit_key: str) -> bool:
    return util.ignored_characters.find(unit_key) == -1 and (not config.kanjionly or util.is_kanji(unit_key))


def contains_kanji(text: str) -> bool:
    return any(util.is_kanji(ch) for ch in text)


def external_unit_score(unit, config: types.SimpleNamespace) -> float:
    if unit.avg_interval <= -GSM_ENCOUNTER_MARKER:
        return min((abs(unit.avg_interval) - GSM_ENCOUNTER_MARKER) / EXTERNAL_SCORE_SCALE, 1)
    if unit.avg_interval <= -JITEN_INTERVAL_MARKER:
        interval = abs(unit.avg_interval) - JITEN_INTERVAL_MARKER
        return util.score_adjust(interval / config.interval)
    if unit.avg_interval < 0:
        return min(abs(unit.avg_interval) / 5, 1)
    return util.score_adjust(unit.avg_interval / config.interval)


def add_textfile_units(units: dict, config: types.SimpleNamespace, anki_priority: bool = False) -> dict:
    source_path = getattr(config, "textsourcepath", "")
    if not source_path or not os.path.isfile(source_path):
        return units

    counts = {}
    first_seen = {}
    with open(source_path, "r", encoding="utf-8-sig", errors="replace") as file_in:
        for idx, line in enumerate(file_in, start=1):
            word = line.strip()
            if not word:
                continue
            for ch in set(word):
                if not valid_unit_key(config, ch):
                    continue
                counts[ch] = counts.get(ch, 0) + 1
                first_seen[ch] = min(first_seen.get(ch, idx), idx)

    for ch, count in counts.items():
        if anki_priority and ch in units:
            continue
        units[ch] = util.unit_tuple(first_seen[ch], ch, -float(count), count, 0)

    return units


def add_gsm_csv_units(units: dict, config: types.SimpleNamespace, anki_priority: bool = False) -> dict:
    source_path = getattr(config, "gsmsourcepath", "")
    if not source_path or not os.path.isfile(source_path):
        return units

    field_limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(field_limit)
            break
        except OverflowError:
            field_limit = int(field_limit / 10)

    aggregate = {}
    with open(source_path, "r", encoding="utf-8-sig", newline="", errors="replace") as csv_in:
        reader = csv.DictReader(csv_in)
        for idx, row in enumerate(reader, start=1):
            word = (row.get("word") or "").strip()
            if not word:
                continue
            try:
                encounters = int(float(row.get("frequency") or 1))
            except ValueError:
                encounters = 1
            encounters = max(encounters, 1)

            for ch in set(word):
                if not valid_unit_key(config, ch):
                    continue
                data = aggregate.setdefault(ch, {"idx": idx, "count": 0})
                data["idx"] = min(data["idx"], idx)
                data["count"] += encounters

    if not aggregate:
        return units

    max_count = max(data["count"] for data in aggregate.values())
    for ch, data in aggregate.items():
        if anki_priority and ch in units:
            continue
        score = 1 if max_count <= 1 else (math.log1p(data["count"]) / math.log1p(max_count))
        units[ch] = util.unit_tuple(
            data["idx"],
            ch,
            -(GSM_ENCOUNTER_MARKER + (score * EXTERNAL_SCORE_SCALE)),
            data["count"],
            0,
        )

    return units


def jmdict_cache_path() -> str:
    return os.path.join(os.path.dirname(__file__), "user_files", "jmdict_sequence_cache.json")


def load_jmdict_sequence_map(config: types.SimpleNamespace) -> dict:
    jmdict_path = getattr(config, "jmdictpath", "")
    if not jmdict_path or not os.path.isfile(jmdict_path):
        return {}

    stat = os.stat(jmdict_path)
    cache_path = jmdict_cache_path()
    try:
        with open(cache_path, "r", encoding="utf-8") as cache_in:
            cache = json.load(cache_in)
        if cache.get("source") == jmdict_path and cache.get("mtime") == stat.st_mtime and cache.get("size") == stat.st_size:
            return {int(k): v for k, v in cache.get("mapping", {}).items()}
    except Exception:  # noqa: BLE001
        pass

    mapping = {}
    with zipfile.ZipFile(jmdict_path) as zip_in:
        term_banks = sorted(name for name in zip_in.namelist() if name.startswith("term_bank_") and name.endswith(".json"))
        for term_bank in term_banks:
            entries = json.loads(zip_in.read(term_bank).decode("utf-8"))
            for entry in entries:
                if len(entry) > 6 and isinstance(entry[6], int):
                    existing = mapping.get(entry[6])
                    if existing is None or (not contains_kanji(existing) and contains_kanji(entry[0])):
                        mapping[entry[6]] = entry[0]

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    try:
        with open(cache_path, "w", encoding="utf-8") as cache_out:
            json.dump({"source": jmdict_path, "mtime": stat.st_mtime, "size": stat.st_size, "mapping": mapping}, cache_out, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return mapping


def add_jiten_backup_units(units: dict, config: types.SimpleNamespace, anki_priority: bool = False) -> dict:
    source_path = getattr(config, "textsourcepath", "")
    if not source_path or not os.path.isfile(source_path):
        return units

    sequence_map = load_jmdict_sequence_map(config)
    if not sequence_map:
        return units

    with open(source_path, "r", encoding="utf-8-sig") as backup_in:
        backup = json.load(backup_in)

    aggregate = {}
    for idx, card in enumerate(backup.get("cards", []), start=1):
        word = sequence_map.get(card.get("w"))
        if not word:
            continue

        interval = card.get("st")
        if interval is None:
            interval = max((card.get("du", 0) - card.get("lr", 0)) / 86400, 1) if card.get("lr") and card.get("du") else 1

        for ch in set(word):
            if not valid_unit_key(config, ch):
                continue
            data = aggregate.setdefault(ch, {"idx": idx, "total": 0.0, "count": 0})
            data["idx"] = min(data["idx"], idx)
            data["total"] += float(interval)
            data["count"] += 1

    for ch, data in aggregate.items():
        if anki_priority and ch in units:
            continue
        avg_interval = data["total"] / data["count"]
        units[ch] = util.unit_tuple(data["idx"], ch, -(JITEN_INTERVAL_MARKER + avg_interval), data["count"], 0)

    return units


def textfile_grid(config: types.SimpleNamespace):
    if getattr(config, "textsourcekind", "txt") == "jiten":
        return add_jiten_backup_units({}, config)
    return add_textfile_units({}, config)


def get_grouping_overall_total(units_list: list, grouping: data.KanjiGrouping, config: types.SimpleNamespace) -> str:
    total_count = 0
    overall_count_known = 0
    grouping_count_known = 0
    grouping_unique_characters = set("".join(group.characters for group in grouping.groups))
    grouping_unique_characters_count = len(grouping_unique_characters)
    for unit in units_list:
        in_grouping = unit.value in grouping_unique_characters

        if unit.seen_cards_count != 0 or config.unseen:
            total_count += 1
            bgcolor = util.get_background_color(unit.avg_interval, config.interval, unit.seen_cards_count, config.gradientcolors, config.kanjitileunseencolor)
            if unit.seen_cards_count != 0 or bgcolor not in [config.gradientcolors[0], config.kanjitileunseencolor]:
                overall_count_known += 1
                if in_grouping:
                    grouping_count_known += 1

    percent_known_overall = "{:.2f}".format(round(overall_count_known / (total_count if total_count > 0 else 1) * 100, 2)) + "%"
    percent_known_grouping = "{:.2f}".format(round(grouping_count_known / (grouping_unique_characters_count if grouping_unique_characters_count > 0 else 1) * 100, 2)) + "%"
    if overall_count_known == 0:
        percent_known_overall = "0%"
        percent_known_grouping = "0%"

    overall_total = "<h4>" + str(overall_count_known) + " of " + str(total_count) + " Known Overall - " + percent_known_overall + "<br>\n"
    within_grouping_total = str(grouping_count_known) + " of " + str(grouping_unique_characters_count) + " Known in Grouping - " + percent_known_grouping + "</h4>\n"
    return overall_total + within_grouping_total

def generate(mw, config: types.SimpleNamespace, units, export: bool = False) -> str:
    def unit_score(unit) -> float:
        return external_unit_score(unit, config)

    def kanjitile(char: str, bgcolor: str, seen_cards_count: int = 0, unseen_cards_count: int = 0, avg_interval: int = 0) -> str:
        tile = ""

        context_menu_events = f" onmouseenter=\"bridgeCommand('h:{char}');\" onmouseleave=\"bridgeCommand('l:{char}');\"" if not export else ""

        if config.tooltips:
            tooltip = "Character: %s" % util.safe_unicodedata_name(char)
            if avg_interval <= -GSM_ENCOUNTER_MARKER:
                tooltip += " | GSM Encounters: " + str(seen_cards_count)
            elif avg_interval <= -JITEN_INTERVAL_MARKER:
                interval = abs(avg_interval) - JITEN_INTERVAL_MARKER
                tooltip += " | Jiten Avg Interval: " + str("{:.2f}".format(interval))
            elif avg_interval < 0:
                tooltip += " | TXT Words: " + str(abs(int(avg_interval)))
            elif avg_interval:
                tooltip += " | Avg Interval: " + str("{:.2f}".format(avg_interval)) + " | Score: " + str("{:.2f}".format(util.score_adjust(avg_interval / config.interval)))
            tooltip += " | Unseen: " + str(unseen_cards_count) + " | Seen: " + str(seen_cards_count)
            tile += "\t<div class=\"grid-item\" style=\"background:%s;\" title=\"%s\"%s>" % (bgcolor, tooltip, context_menu_events)
        else:
            tile += "\t<div class=\"grid-item\" style=\"background:%s;\"%s>" % (bgcolor, context_menu_events)

        if config.onclickaction == "copy":
            tile += "<a style=\"cursor: pointer;\" class=\"kanji-tile\">" + char + "</a>"
        elif config.onclickaction == "browse" and not export:
            tile += "<a href=\"" + util.get_browse_command(char) + "\" \" class=\"kanji-tile\">" + char + "</a>"
        elif config.onclickaction == "search":
            tile += "<a href=\"" + util.get_search(config, char) + "\" class=\"kanji-tile\">" + char + "</a>"
        else:
            tile += "<span class=\"kanji-tile\">" + char + "</span>"

        tile += "</div>\n"

        return tile

    deckname = "*"
    if getattr(config, "usetextsource", False):
        deckname = os.path.basename(getattr(config, "textsourcepath", "")) or "TXT word list"
    elif config.did != "*":
        deckname = mw.col.decks.name(config.did).rsplit("::", 1)[-1]

    result_html  = "<!doctype html><html lang=\"" + config.lang + "\"><head><meta charset=\"UTF-8\" /><title>Anki Kanji Grid</title>"
    result_html += "<style type=\"text/css\">" + HEADER_CSS_SNIPPET(config)
    result_html += "a, a:visited {color: " + config.kanjitextcolor + ";text-decoration: none;}"
    result_html += ".kanji-tile {color: " + config.kanjitextcolor + "}"
    result_html += "body {color: " + config.textcolor + "}"
    result_html += ".kanji"
    result_html += "</style>"
    if config.onclickaction == "copy":
        result_html += COPY_JS_SNIPPET
    if not export:
        result_html += "<style type=\"text/css\">" + SEARCH_CSS_SNIPPET + "</style>"
        result_html += "<script>" + SEARCH_JS_SNIPPET + "</script>"
    result_html += "</head>\n"
    result_html += "<body>\n"
    result_html += "<div style=\"font-size: 3em;\">Kanji Grid - " + deckname + "</div>\n"
    if config.timetravel_enabled:
        date_time = datetime.datetime.fromtimestamp(config.timetravel_time / 1000, tz = datetime.timezone.utc)
        date_time_str = date_time.strftime("%d/%m/%Y %H:%M:%S")
        result_html += "<p style=\"text-align: center\">for " + date_time_str + "</p>"
    result_html += "<p style=\"text-align: center\">Key</p>"
    result_html += "<p style=\"text-align: center;\">Weak&nbsp;"

    key_css_gradient = "linear-gradient(90deg"
    gradient_key_step_count = 100
    for i in range(0, gradient_key_step_count + 1):
        key_css_gradient += "," + util.get_gradient_color_hex(i / gradient_key_step_count, config.gradientcolors)
    key_css_gradient += ")"
    result_html += "<span class=\"key\" style=\"background: " + key_css_gradient + "; width: 21em;\">&nbsp;</span>"
    result_html += "&nbsp;Strong</p></div>\n"
    if getattr(config, "usetextsource", False):
        external_key_css_gradient = "linear-gradient(90deg"
        for i in range(0, gradient_key_step_count + 1):
            external_key_css_gradient += "," + util.mute_hex_color(util.get_gradient_color_hex(i / gradient_key_step_count, config.gradientcolors))
        external_key_css_gradient += ")"
        result_html += "<p style=\"text-align: center;\">External&nbsp;<span class=\"key\" style=\"background: " + external_key_css_gradient + "; width: 21em;\">&nbsp;</span>&nbsp;Stronger</p>\n"
    if getattr(config, "usegsmsource", False):
        gsm_key_css_gradient = "linear-gradient(90deg"
        for i in range(0, gradient_key_step_count + 1):
            gsm_key_css_gradient += "," + util.get_gradient_color_hex(i / gradient_key_step_count, ["#f0edf6", "#8a5fb5"])
        gsm_key_css_gradient += ")"
        result_html += "<p style=\"text-align: center;\">GSM encounters&nbsp;<span class=\"key\" style=\"background: " + gsm_key_css_gradient + "; width: 21em;\">&nbsp;</span>&nbsp;More encounters</p>\n"
    result_html += "<hr style=\"border-style: dashed;border-color: #666;width: 100%;\">\n"
    result_html += "<div style=\"text-align: center;\">\n"

    units_list = {
        util.SortOrder.NONE:      sorted(units.values(), key=lambda unit: (unit.idx, unit.seen_cards_count)),
        util.SortOrder.UNICODE:   sorted(units.values(), key=lambda unit: (util.safe_unicodedata_name(unit.value), unit.seen_cards_count)),
        util.SortOrder.SCORE:     sorted(units.values(), key=lambda unit: (unit_score(unit), unit.seen_cards_count), reverse=True),
        util.SortOrder.SEEN_CARDS_COUNT: sorted(units.values(), key=lambda unit: (unit.seen_cards_count, unit_score(unit)), reverse=True),
        util.SortOrder.UNSEEN_CARDS_COUNT:  sorted(units.values(), key=lambda unit: (unit.unseen_cards_count), reverse=True),
    }[util.SortOrder(config.sortby)]

    if config.groupby > 0:
        grouping = data.groupings[config.groupby - 1]
        kanji = [u.value for u in units_list]

        result_html += get_grouping_overall_total(units_list, grouping, config)

        for i in range(0, len(grouping.groups)):
            result_html += "<h2>" + grouping.groups[i].name + "</h2>\n"
            table = "<div class=\"grid-container\">\n"
            count_found = 0
            count_known = 0

            sorted_units = []
            if config.sortby == 0:
                sorted_units = [units[c] for c in grouping.groups[i].characters if c in kanji]
            else:
                sorted_units = [units[c] for c in kanji if c in grouping.groups[i].characters]

            for unit in sorted_units:
                if unit.seen_cards_count != 0 or config.unseen:
                    count_found += 1
                    bgcolor = util.get_background_color(unit.avg_interval, config.interval, unit.seen_cards_count, config.gradientcolors, config.kanjitileunseencolor)
                    if unit.seen_cards_count != 0 or bgcolor not in [config.gradientcolors[0], config.kanjitileunseencolor]:
                        count_known += 1
                    table += kanjitile(unit.value, bgcolor, unit.seen_cards_count, unit.unseen_cards_count, unit.avg_interval)
            table += "</div>\n"
            total_count = len(grouping.groups[i].characters)
            if config.unseen:
                unseen_kanji = []
                count = 0
                for char in [c for c in grouping.groups[i].characters if c not in kanji]:
                    count += 1
                    bgcolor = config.kanjitilemissingcolor
                    unseen_kanji.append(kanjitile(char, bgcolor))
                if count != 0:
                    table += "<details><summary>Missing kanji</summary><div class=\"grid-container\">\n"
                    for element in unseen_kanji:
                        table += element
                    table += "</div></details>\n"
            result_html += "<h4>" + str(count_found) + " of " + str(total_count) + " Found - " + "{:.2f}".format(round(count_found / (total_count if total_count > 0 else 1) * 100, 2)) + "%, " + str(count_known) + " of " + str(total_count) + " Known - " + "{:.2f}".format(round(count_known / (total_count if total_count > 0 else 1) * 100, 2)) + "%</h4>\n"
            result_html += table

        chars = reduce(lambda x, y: x+y, dict(grouping.groups).values())
        result_html += "<h2>" + grouping.leftover_group + "</h2>" #label for "not in group" groups
        table = "<div class=\"grid-container\">\n"
        total_count = 0
        count_known = 0
        for unit in [u for u in units_list if u.value not in chars]:
            if unit.seen_cards_count != 0 or config.unseen:
                total_count += 1
                bgcolor = util.get_background_color(unit.avg_interval, config.interval, unit.seen_cards_count, config.gradientcolors, config.kanjitileunseencolor)
                if unit.seen_cards_count != 0 or bgcolor not in [config.gradientcolors[0], config.kanjitileunseencolor]:
                    count_known += 1
                table += kanjitile(unit.value, bgcolor, unit.seen_cards_count, unit.unseen_cards_count, unit.avg_interval)
        table += "</div>\n"
        result_html += "<h4>" + str(count_known) + " of " + str(total_count) + " Known - " + "{:.2f}".format(round(count_known / (total_count if total_count > 0 else 1) * 100, 2)) + "%</h4>\n"
        result_html += table
        result_html += "<style type=\"text/css\">.datasource{font-style:italic;font-size:0.75em;margin-top:1em;overflow-wrap:break-word;}.datasource a{color:#1034A6;}</style><span class=\"datasource\">Data source: " + ' '.join("<a href=\"{}\">{}</a>".format(w, urllib.parse.unquote(w)) if re.match("https?://", w) else w for w in grouping.source.split(' ')) + "</span>"
    else:
        table = "<div class=\"grid-container\">\n"
        total_count = 0
        count_known = 0
        for unit in units_list:
            if unit.seen_cards_count != 0 or config.unseen:
                total_count += 1
                bgcolor = util.get_background_color(unit.avg_interval, config.interval, unit.seen_cards_count, config.gradientcolors, config.kanjitileunseencolor)
                if unit.seen_cards_count != 0 or bgcolor not in [config.gradientcolors[0], config.kanjitileunseencolor]:
                    count_known += 1
                table += kanjitile(unit.value, bgcolor, unit.seen_cards_count, unit.unseen_cards_count, unit.avg_interval)
        table += "</div>\n"

        known_percent = "{:.2f}".format(round(count_known / (total_count if total_count > 0 else 1) * 100, 2)) + "%"
        if count_known == 0:
            known_percent = "0%"
        result_html += "<h4>" + str(count_known) + " of " + str(total_count) + " Known - " + known_percent + "</h4>\n"
        result_html += table
    result_html += "</div></body></html>\n"
    return result_html

def get_revlog(mw, cids, timetravel_time):
    # SQLITE_MAX_SQL_LENGTH is 1GB by default
    # so it could handle ~70 million ids
    # but chunk just in case
    CIDS_CHUNK = 50000
    revlog = {}
    for i in range(0, len(cids), CIDS_CHUNK):
        chunked_cids = cids[i:i+CIDS_CHUNK]
        revlog_rows = mw.col.db.all(f"""
            select cid, max(id), ivl
            from revlog 
            where id <= {timetravel_time}
            and cid in {ids2str(chunked_cids)}
            group by cid
        """)
        revlog |= {row[0]: row[2] for row in revlog_rows} # cid -> ivl

    return revlog

def timetravel(card, revlog, timetravel_time):
    if card.id not in revlog:
        # card was not reviewed during the timeframe...
        if card.id > timetravel_time:
            # ...and wasn't in deck either, so it shouldn't be counted
            return False
        # ...but was still present in deck,
        # so patch it to be of type "new" (to emulate existing behaviour)
        card.ivl = 0
        card.type = 0
    else:
        # a negative revlog ivl is in seconds, positive is in days
        revlog_ivl = revlog[card.id]
        card.ivl = revlog_ivl if revlog_ivl >= 0 else (-revlog_ivl) // (60 * 60 * 24)

    return True

def kanjigrid(mw, config: types.SimpleNamespace):
    if getattr(config, "usetextsource", False) and not getattr(config, "mixtextsource", False):
        units = textfile_grid(config)
        if getattr(config, "usegsmsource", False):
            add_gsm_csv_units(units, config, anki_priority=True)
        return units

    dids = [config.did]
    if config.did == "*":
        dids = mw.col.decks.all_ids()
    for deck_id in dids:
        for _, id_ in mw.col.decks.children(int(deck_id)):
            dids.append(id_)
    cids = []
    #mw.col.find_cards and mw.col.db.list sort differently
    #mw.col.db.list is kept due to some users being very picky about the order of kanji when using `Sort by: None`
    if len(config.searchfilter) > 0 and len(config.fieldslist) > 0 and len(dids) > 0:
        cids = mw.col.find_cards("(" + util.make_query(dids, config.fieldslist) + ") " + config.searchfilter)
    else:
        cids = mw.col.db.list("select id from cards where did in %s or odid in %s" % (ids2str(dids), ids2str(dids)))

    timetravel_enabled = config.timetravel_enabled
    timetravel_time = config.timetravel_time
    revlog = get_revlog(mw, cids, timetravel_time) if timetravel_enabled else {}

    units = {}
    notes = {}
    for i in cids:
        card = mw.col.get_card(i)
        # tradeoff between branching and mutating here vs collecting all the cards and then filtermapping 
        if timetravel_enabled and not timetravel(card, revlog, timetravel_time):
            continue # ignore card
        if card.nid not in notes:
            keys = card.note().keys()
            unit_key = set()
            matches = operator.eq
            for keyword in config.fieldslist:
                for key in keys:
                    if matches(key.lower(), keyword):
                        unit_key.update(set(card.note()[key]))
                        break
            notes[card.nid] = unit_key
        else:
            unit_key = notes[card.nid]
        if unit_key is not None:
            for ch in unit_key:
                util.add_unit_data(units, ch, i, card, config.kanjionly)
    if getattr(config, "usetextsource", False) and getattr(config, "mixtextsource", False):
        if getattr(config, "textsourcekind", "txt") == "jiten":
            add_jiten_backup_units(units, config, anki_priority=True)
        else:
            add_textfile_units(units, config, anki_priority=True)
    if getattr(config, "usegsmsource", False):
        add_gsm_csv_units(units, config, anki_priority=True)
    return units

HEADER_CSS_SNIPPET = lambda config: ("""
body {
  text-align: center;
}

.grid-container {
  display: grid;
  grid-gap: 2px;
  grid-template-columns: repeat(auto-fit, 23px);
  justify-content: center;
  """ + util.get_font_css(config) + """
}

.key {
  display: inline-block;
  width: 3em
}
""").strip()

SEARCH_CSS_SNIPPET = """
.grid-item.highlight {
  background: black !important; /* override item's inline interval colour */
}

.grid-item.highlight > * {
  color: white !important; /* override inline style */
}

.blink {
  animation: blink 0.2s ease-in-out;
  animation-iteration-count: 2;
}

@keyframes blink {
  0% { opacity: 1; }
  50% { opacity: 0; }
  100% { opacity: 1; }
}
""".strip()

SEARCH_JS_SNIPPET = """
function findChar(char) {
  const GRID_ITEM_CLASS = "grid-item";
  const HIGHLIGHT_CLASS = "highlight";
  const ANIM_CLASS = "blink";

  /* clear the previous match's highlight (if any) */
  const prevMatchingElem = document.querySelector(`.${HIGHLIGHT_CLASS}`);
  if (prevMatchingElem !== null) {
    prevMatchingElem.classList.remove(HIGHLIGHT_CLASS);
  }

  /* selects the first matching grid item, so it assumes the grid kanji are unique */
  /* according to mdn, more specific xpath exprs are faster, esp. on larger grids */
  const xpath = `.//div[contains(@class, '${GRID_ITEM_CLASS}')][*[.='${char}']]`;
  const matchingElement = document.evaluate(xpath, document.body, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;

  if (matchingElement === null) {
    return false;
  }

  /* we need to open the enclosing <details> block first (if any), or scrollIntoView won't work */
  const parentDetailsElem = matchingElement.closest('details');
  if (parentDetailsElem !== null) {
    parentDetailsElem.open = true;
  }

  /* add our own highlight style to the current match */
  matchingElement.classList.add(HIGHLIGHT_CLASS);

  /* scroll to match */
  matchingElement.scrollIntoView({ behavior: "smooth", block: "center" });

  /* blink anim with cleanup */
  matchingElement.classList.add(ANIM_CLASS);
  matchingElement.addEventListener("animationend", function() {
    matchingElement.classList.remove(ANIM_CLASS);
  }, { once: true });

  /* ret value indicates whether a match was found */
  return true;
}
""".strip()

COPY_JS_SNIPPET = """
<script>
    function copyText(text) {
        const range = document.createRange();
        const tempElem = document.createElement('div');
        tempElem.textContent = text;
        document.body.appendChild(tempElem);
        range.selectNode(tempElem);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('copy');
        document.body.removeChild(tempElem);
    }
    document.addEventListener('click', function(e) {
        if (e.srcElement.tagName == 'A') {
            e.preventDefault();
            copyText(e.srcElement.textContent);
        }
    }, false);
</script>
""".strip()
