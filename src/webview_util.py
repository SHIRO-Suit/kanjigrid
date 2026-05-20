import shlex
import re
import traceback
import types
import json
import os
from enum import Enum

from aqt import dialogs, mw
from aqt.qt import QApplication, QTimer, qconnect
from aqt.utils import showCritical, tooltip
from aqt.webview import AnkiWebView

from . import config_util, data, generate_grid, logger, util


# mimic `AnkiWebViewKind`
# https://github.com/ankitects/anki/blob/1a68c9f5d5bcc197b641fe7405e5d9a4823928f3/qt/aqt/webview.py#L34-L59
class KanjiGridWebViewKind(Enum):
    DEFAULT = "kanjigrid webview"

STUDY_DECK_NAME_PREFIX = "unseen kanjis from grid group "
TEMP_STUDY_DECK_NAME_PREFIX = "Temp - " + STUDY_DECK_NAME_PREFIX
MAX_STUDY_DECK_QUERY_LENGTH = 65000
MAX_STUDY_DECK_SPLIT_QUERY_LENGTH = 12000
DYNAMIC_STUDY_DECK_LIMIT = 99999
STUDY_DECK_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "user_files", "study_deck_configs.json")
startup_update_scheduled = False
study_deck_note_watch_timer = None
study_deck_note_watch_last_id = None
STUDY_DECK_CONFIG_KEYS = (
    "defaultdeck",
    "defaultfield",
    "fieldslist",
    "groupby",
    "lang",
    "searchfilter",
    "kanjionly",
    "unseen",
    "usequerystudydeck",
    "splitbigdynamicqueries",
)

def init_webview() -> None:
    webview = AnkiWebView()
    try:
        webview.set_kind(KanjiGridWebViewKind.DEFAULT)
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to set webview kind", traceback.format_exc())
    return webview

def open_search_link(wv: AnkiWebView, config: types.SimpleNamespace, char: str) -> None:
    link = util.get_search(config, char)
    # aqt.utils.openLink is an alternative
    wv.eval(f"window.open('{link}', '_blank');")

def open_note_browser(deckname: str, fields_list: list, additional_search_filters: str, search_string: str) -> None:
    fields_string = ""
    for i, field in enumerate(fields_list):
        if i != 0:
            fields_string += " OR "
        fields_string += field + ":*" + search_string + "*"
    if len(fields_list) > 1:
        fields_string = "(" + fields_string + ")"
    browser = dialogs.open("Browser", mw)
    browser.form.searchEdit.lineEdit().setText("deck:\"" + deckname + "\" " + fields_string + " " + additional_search_filters)
    browser.onSearchActivated()

def on_copy_cmd(char: str) -> None:
    QApplication.clipboard().setText(char)

def on_browse_cmd(char: str, config: types.SimpleNamespace, deckname: str) -> None:
    open_note_browser(deckname, config.fieldslist, config.searchfilter, char)

def study_units_for_group(units: dict, config: types.SimpleNamespace, group_index: int) -> list:
    if config.groupby <= 0:
        return []

    grouping = data.groupings[config.groupby - 1]
    if group_index < len(grouping.groups):
        return [units[c] for c in grouping.groups[group_index].characters if c in units]

    grouped_chars = set("".join(group.characters for group in grouping.groups))
    return [unit for unit in units.values() if unit.value not in grouped_chars]

def unseen_card_ids_for_study_units(study_units: list) -> list:
    card_ids = []
    for unit in study_units:
        if unit.seen_cards_count == 0 and unit.unseen_cards_count > 0:
            card_ids.extend(unit.unseen_card_ids)
    return sorted(set(card_ids))

def search_chars_for_study_group(units: dict, config: types.SimpleNamespace, group_index: int, group_units: list) -> set:
    chars = {unit.value for unit in group_units if unit.seen_cards_count == 0}
    if config.groupby <= 0:
        return chars

    grouping = data.groupings[config.groupby - 1]
    if group_index >= len(grouping.groups):
        return chars

    for char in grouping.groups[group_index].characters:
        if getattr(config, "kanjionly", True) and not util.is_kanji(char):
            continue
        unit = units.get(char)
        if unit is None or unit.seen_cards_count == 0:
            chars.add(char)
    return chars

def quote_search_term(term: str) -> str:
    escaped = term.replace('\\', '\\\\').replace('"', '\\"')
    return escaped


def normalize_fields_list(fields: list) -> list:
    if fields is None:
        return []
    return [field.strip() for field in fields if field is not None and field.strip() != ""]


def field_contains_kanji_query(fields: list, chars: set) -> str:
    query_terms = []
    for field in normalize_fields_list(fields):
        escaped_field = quote_search_term(field)
        for char in sorted(chars):
            query_terms.append(f"\"{escaped_field}:*{char}*\"")
    return " OR ".join(query_terms)


def build_group_search_query(config: types.SimpleNamespace, fields_list: list, chars: list) -> str:
    field_query = field_contains_kanji_query(fields_list, set(chars))
    if not field_query:
        return None

    search_parts = ["is:new"]
    if getattr(config, "did", "*") != "*":
        deck_name = mw.col.decks.name(config.did)
        deck_name = quote_search_term(deck_name)
        search_parts.append(f'deck:"{deck_name}"')

    search_parts.append("(" + field_query + ")")

    additional_filter = getattr(config, "searchfilter", "").strip()
    if additional_filter:
        search_parts.append(additional_filter)

    return " ".join(search_parts)


def split_group_search_queries(config: types.SimpleNamespace, group_index: int, chars: set) -> list:
    if not getattr(config, "usequerystudydeck", True):
        return None

    fields_list = normalize_fields_list(getattr(config, "fieldslist", None))
    if len(fields_list) == 0:
        fields_list = normalize_fields_list(shlex.split(getattr(config, "defaultfield", "")))
    if len(chars) == 0 or len(fields_list) == 0:
        return None

    searches = []
    current_chars = []
    current_search = None
    for char in sorted(chars):
        candidate_chars = current_chars + [char]
        candidate_search = build_group_search_query(config, fields_list, candidate_chars)
        if candidate_search is None:
            continue
        if len(candidate_search) <= MAX_STUDY_DECK_SPLIT_QUERY_LENGTH:
            current_chars = candidate_chars
            current_search = candidate_search
            continue

        if current_search is not None:
            searches.append(current_search)
            current_chars = [char]
            current_search = build_group_search_query(config, fields_list, current_chars)
            if current_search is None or len(current_search) > MAX_STUDY_DECK_SPLIT_QUERY_LENGTH:
                return None
        else:
            return None

    if current_search is not None:
        searches.append(current_search)

    if len(searches) == 0:
        return None
    return searches


def group_search_query(config: types.SimpleNamespace, group_index: int, chars: set) -> str:
    if not getattr(config, "usequerystudydeck", True):
        return None

    fields_list = normalize_fields_list(getattr(config, "fieldslist", None))
    if len(fields_list) == 0:
        fields_list = normalize_fields_list(shlex.split(getattr(config, "defaultfield", "")))
    if len(chars) == 0 or len(fields_list) == 0:
        return None

    search = build_group_search_query(config, fields_list, sorted(chars))
    if not search:
        return None
    if len(search) > MAX_STUDY_DECK_QUERY_LENGTH:
        return None
    return search


def find_cards_for_searches(searches: list) -> set:
    card_ids = set()
    for search in searches:
        card_ids.update(mw.col.find_cards(search))
    return card_ids


def cid_search(card_ids: list) -> str:
    if len(card_ids) == 0:
        return "cid:0 is:new"
    return "cid:" + ",".join(str(card_id) for card_id in card_ids) + " is:new"

def notify_query_fallback(group_name: str, error_message: str = None) -> None:
    message = f"Kanji Grid query failed for {group_name}; falling back to card IDs"
    if error_message:
        message += f": {error_message}"
    tooltip(message)
    logger.log(message)


def card_ids_from_cid_search(search: str) -> set:
    card_ids = set()
    for match in re.finditer(r"\bcid:([0-9,]+)", search or ""):
        for card_id in match.group(1).split(","):
            try:
                parsed_id = int(card_id)
            except ValueError:
                continue
            if parsed_id:
                card_ids.add(parsed_id)
    return card_ids

def is_static_study_deck(deck: dict) -> bool:
    terms = deck.get("terms", [])
    if len(terms) == 0 or len(terms[0]) == 0:
        return False
    return bool(card_ids_from_cid_search(terms[0][0]))

def is_dynamic_study_deck(deck: dict) -> bool:
    return not is_static_study_deck(deck)

def study_deck_batch_size(config: types.SimpleNamespace) -> int:
    try:
        return max(1, int(getattr(config, "studydeckbatchsize", 10)))
    except (TypeError, ValueError):
        return 10

def study_deck_limit_for_search(search: str, card_count: int, config: types.SimpleNamespace) -> int:
    return study_deck_batch_size(config)

def existing_study_deck_limit(deck: dict):
    terms = deck.get("terms", [])
    if len(terms) == 0 or len(terms[0]) < 2:
        return None
    try:
        return max(1, int(terms[0][1]))
    except (TypeError, ValueError):
        return None

def study_deck_limit_for_update(search: str, card_count: int, config: types.SimpleNamespace, deck: dict) -> int:
    if getattr(config, "updatestudydeckbatchsize", False):
        return study_deck_limit_for_search(search, card_count, config)
    existing_limit = existing_study_deck_limit(deck)
    if existing_limit is not None:
        return existing_limit
    return study_deck_limit_for_search(search, card_count, config)

def study_deck_loaded_count(card_count: int, config: types.SimpleNamespace) -> int:
    return min(card_count, study_deck_batch_size(config))

def ensure_dynamic_study_deck_limit(deck: dict, config: types.SimpleNamespace) -> None:
    if is_static_study_deck(deck):
        return
    if not getattr(config, "updatestudydeckbatchsize", False):
        return
    terms = deck.get("terms", [])
    batch_size = study_deck_batch_size(config)
    changed = False
    for term in terms:
        if len(term) < 2:
            continue
        if term[1] != batch_size:
            term[1] = batch_size
            changed = True
    if changed:
        deck["terms"] = terms
        save_deck(deck)

def save_deck(deck: dict) -> None:
    if hasattr(mw.col.decks, "save"):
        mw.col.decks.save(deck)
    else:
        mw.col.decks.update(deck)

def empty_filtered_deck(deck_id: int) -> None:
    if hasattr(mw.col.sched, "emptyDyn"):
        mw.col.sched.emptyDyn(deck_id)
    elif hasattr(mw.col.sched, "empty_filtered_deck"):
        mw.col.sched.empty_filtered_deck(deck_id)

def rebuild_filtered_deck(deck_id: int) -> None:
    if hasattr(mw.col.sched, "rebuildDyn"):
        mw.col.sched.rebuildDyn(deck_id)
    elif hasattr(mw.col.sched, "rebuild_filtered_deck"):
        mw.col.sched.rebuild_filtered_deck(deck_id)
    else:
        raise RuntimeError("This Anki version does not expose a filtered deck rebuild API.")

def rebuild_existing_filtered_deck(deck_id: int) -> None:
    empty_filtered_deck(deck_id)
    rebuild_filtered_deck(deck_id)

def refresh_deck_layout() -> None:
    if hasattr(mw, "deckBrowser") and hasattr(mw.deckBrowser, "refresh"):
        mw.deckBrowser.refresh()
    elif hasattr(mw, "reset"):
        mw.reset()

def study_deck_name(group_name: str, temporary: bool) -> str:
    name = STUDY_DECK_NAME_PREFIX + "\"" + group_name + "\""
    if temporary:
        name = "Temp - " + name
    return name

def split_study_deck_name(group_name: str, temporary: bool, index: int, total: int) -> str:
    return study_deck_name(group_name, temporary) + f" {index}/{total}"

def split_study_deck_index(deck_name: str):
    match = re.search(r"\s+(\d+)/(\d+)$", deck_name)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))

def study_group_name(config: types.SimpleNamespace, group_index: int) -> str:
    if config.groupby <= 0:
        return "All"

    grouping = data.groupings[config.groupby - 1]
    if group_index < len(grouping.groups):
        return grouping.groups[group_index].name
    return grouping.leftover_group

def study_deck_group_name(deck_name: str):
    if deck_name.startswith(TEMP_STUDY_DECK_NAME_PREFIX):
        name = deck_name[len(TEMP_STUDY_DECK_NAME_PREFIX):]
        name = re.sub(r"\s+\d+/\d+$", "", name)
        return name.strip('"')
    if deck_name.startswith(STUDY_DECK_NAME_PREFIX):
        name = deck_name[len(STUDY_DECK_NAME_PREFIX):]
        name = re.sub(r"\s+\d+/\d+$", "", name)
        return name.strip('"')
    return None

def tracked_study_decks() -> list:
    decks = []
    for deck in mw.col.decks.all():
        if deck.get("dyn") and study_deck_group_name(deck.get("name", "")) is not None:
            decks.append(deck)
    return sorted(decks, key=lambda deck: deck.get("name", ""))

def tracked_study_deck_summaries() -> list:
    summaries = []
    for deck in tracked_study_decks():
        kind = "Dynamic query" if is_dynamic_study_deck(deck) else "Static card IDs"
        try:
            count = mw.col.db.scalar("select count() from cards where did = ?", deck["id"])
        except Exception:  # noqa: BLE001
            count = 0
        summaries.append(f"{deck.get('name', '')} - {kind} - {count} card(s)")
    return summaries

def filtered_deck_by_name(name: str):
    deck = mw.col.decks.by_name(name)
    if deck and deck.get("dyn"):
        return deck
    return None

def filtered_study_deck_for_group(config: types.SimpleNamespace, group_index: int):
    group_name = study_group_name(config, group_index)
    deck = filtered_deck_by_name(study_deck_name(group_name, False))
    if deck is not None:
        return deck
    deck = filtered_deck_by_name(study_deck_name(group_name, True))
    if deck is not None:
        return deck
    for deck in tracked_study_decks():
        if study_deck_group_name(deck.get("name", "")) == group_name:
            return deck
    return None

def filtered_permanent_study_deck_for_group_name(group_name: str):
    deck = filtered_deck_by_name(study_deck_name(group_name, False))
    if deck is not None:
        return deck
    for deck in tracked_study_decks():
        name = deck.get("name", "")
        if name.startswith(STUDY_DECK_NAME_PREFIX) and study_deck_group_name(name) == group_name:
            return deck
    return None

def filtered_static_study_deck_for_group(config: types.SimpleNamespace, group_index: int):
    deck = filtered_study_deck_for_group(config, group_index)
    if deck is not None and is_static_study_deck(deck):
        return deck
    return None

def deck_exists(deck_id) -> bool:
    if deck_id == "*":
        return True
    try:
        return mw.col.decks.get(int(deck_id)) is not None
    except Exception:  # noqa: BLE001
        return False

def normalize_study_config(config: types.SimpleNamespace) -> types.SimpleNamespace:
    default_deck = getattr(config, "defaultdeck", "")
    if default_deck == "*":
        config.did = "*"
    elif default_deck:
        selected_deck = mw.col.decks.by_name(default_deck)
        config.did = selected_deck["id"] if selected_deck else "*"
    elif not hasattr(config, "did") or not deck_exists(config.did):
        current_deck = mw.col.conf.get("curDeck", "*")
        config.did = current_deck if deck_exists(current_deck) else "*"

    if not hasattr(config, "fieldslist"):
        config.fieldslist = normalize_fields_list(shlex.split(getattr(config, "defaultfield", "")))
    else:
        config.fieldslist = normalize_fields_list(config.fieldslist)

    config.timetravel_enabled = False
    config.timetravel_time = 0
    config.usetextsource = False
    config.usejitenapi = False
    config.usegsmsource = False
    config.usegsmapi = False
    return config

def study_deck_config_snapshot(config: types.SimpleNamespace, group_name: str) -> dict:
    snapshot = {}
    for key in STUDY_DECK_CONFIG_KEYS:
        if hasattr(config, key):
            value = getattr(config, key)
            snapshot[key] = list(value) if isinstance(value, tuple) else value
    snapshot["group_name"] = group_name
    return snapshot

def base_study_deck_name_from_split(deck_name: str) -> str:
    return re.sub(r"\s+\d+/\d+$", "", deck_name)

def read_study_deck_config_file() -> dict:
    try:
        if not os.path.exists(STUDY_DECK_CONFIG_PATH):
            return {}
        with open(STUDY_DECK_CONFIG_PATH, "r", encoding="utf8") as config_file:
            mapping = json.load(config_file)
        return mapping if isinstance(mapping, dict) else {}
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to read Kanji Grid study deck config file", traceback.format_exc())
        return {}

def write_study_deck_config_file(mapping: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STUDY_DECK_CONFIG_PATH), exist_ok=True)
        with open(STUDY_DECK_CONFIG_PATH, "w", encoding="utf8") as config_file:
            json.dump(mapping, config_file, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to write Kanji Grid study deck config file", traceback.format_exc())

def legacy_study_deck_config_map() -> dict:
    saved_config = types.SimpleNamespace(**config_util.get_config(mw))
    mapping = getattr(saved_config, "studydeckconfigs", {})
    return mapping if isinstance(mapping, dict) else {}

def clean_study_deck_config_map(mapping: dict) -> dict:
    existing_names = {deck.get("name", "") for deck in tracked_study_decks()}
    return {name: snapshot for name, snapshot in mapping.items() if name in existing_names}

def saved_study_deck_config_map() -> dict:
    mapping = read_study_deck_config_file()
    legacy_mapping = legacy_study_deck_config_map()
    if legacy_mapping:
        mapping = {**legacy_mapping, **mapping}
        write_study_deck_config_file(mapping)
        saved_config = types.SimpleNamespace(**config_util.get_config(mw))
        if hasattr(saved_config, "studydeckconfigs"):
            delattr(saved_config, "studydeckconfigs")
            config_util.set_config(mw, saved_config)
    return mapping if isinstance(mapping, dict) else {}

def clean_saved_study_deck_configs() -> None:
    mapping = saved_study_deck_config_map()
    cleaned = clean_study_deck_config_map(mapping)
    if cleaned != mapping:
        write_study_deck_config_file(cleaned)

def save_study_deck_configs(deck_names: list, config: types.SimpleNamespace, group_name: str) -> None:
    mapping = saved_study_deck_config_map()
    snapshot = study_deck_config_snapshot(config, group_name)
    for deck_name in deck_names:
        mapping[deck_name] = snapshot
    write_study_deck_config_file(clean_study_deck_config_map(mapping))

def config_for_study_deck(deck: dict, fallback_config: types.SimpleNamespace = None) -> types.SimpleNamespace:
    mapping = saved_study_deck_config_map()
    deck_name = deck.get("name", "")
    snapshot = mapping.get(deck_name) or mapping.get(base_study_deck_name_from_split(deck_name))
    if snapshot is None:
        snapshot = fallback_config.__dict__ if fallback_config is not None else config_util.get_config(mw)
    current_config = fallback_config if fallback_config is not None else types.SimpleNamespace(**config_util.get_config(mw))
    config = types.SimpleNamespace(**dict(snapshot))
    config.studydeckbatchsize = getattr(current_config, "studydeckbatchsize", 10)
    config.updatestudydeckbatchsize = getattr(current_config, "updatestudydeckbatchsize", False)
    if hasattr(config, "group_name"):
        delattr(config, "group_name")
    return normalize_study_config(config)

def study_deck_source_dids(config: types.SimpleNamespace) -> list:
    if getattr(config, "did", "*") == "*":
        dids = list(mw.col.decks.all_ids())
    else:
        dids = [int(config.did)]

    seen = set(dids)
    for deck_id in list(dids):
        for _, child_id in mw.col.decks.children(int(deck_id)):
            if child_id not in seen:
                dids.append(child_id)
                seen.add(child_id)
    return dids

def note_card_ids_matching_grid_scope(note_id: int, config: types.SimpleNamespace) -> set:
    dids = study_deck_source_dids(config)
    if len(dids) == 0:
        return set()

    if len(getattr(config, "searchfilter", "")) > 0 and len(getattr(config, "fieldslist", [])) > 0:
        query = "(" + util.make_query(dids, config.fieldslist) + f") ({config.searchfilter}) nid:{note_id} is:new"
        try:
            return set(mw.col.find_cards(query))
        except Exception:  # noqa: BLE001
            logger.error_log("Failed to search new note cards for Kanji Grid study deck", traceback.format_exc())
            return set()

    return set(mw.col.db.list(
        "select id from cards where nid = ? and type = 0 and (did in %s or odid in %s)"
        % (generate_grid.ids2str(dids), generate_grid.ids2str(dids)),
        note_id,
    ))

def study_group_index(config: types.SimpleNamespace, group_name: str):
    if config.groupby <= 0:
        return 0 if group_name == "All" else None

    grouping = data.groupings[config.groupby - 1]
    for index, group in enumerate(grouping.groups):
        if group.name == group_name:
            return index
    if grouping.leftover_group == group_name:
        return len(grouping.groups)
    return None

def create_or_update_filtered_deck(name: str, search: str, limit: int, config: types.SimpleNamespace) -> int:
    return create_or_update_filtered_deck_with_terms(name, [[search, study_deck_limit_for_search(search, limit, config), 0]])

def create_or_update_filtered_deck_with_terms(name: str, terms: list) -> int:
    if hasattr(mw.col.decks, "newDyn"):
        new_deck = mw.col.decks.newDyn(name)
        if isinstance(new_deck, dict):
            deck = new_deck
        else:
            deck = mw.col.decks.get(new_deck)
    else:
        deck_id = mw.col.decks.id(name)
        deck = mw.col.decks.get(deck_id)
        deck["dyn"] = 1

    if not deck.get("dyn"):
        raise RuntimeError(f"A normal deck named \"{name}\" already exists.")

    deck["terms"] = terms
    deck["resched"] = True
    save_deck(deck)
    empty_filtered_deck(deck["id"])
    rebuild_filtered_deck(deck["id"])
    return deck["id"]

def delete_extra_split_study_decks(group_name: str, temporary: bool, keep_total: int) -> None:
    base = study_deck_name(group_name, temporary)
    for deck in tracked_study_decks():
        name = deck.get("name", "")
        if not name.startswith(base + " "):
            continue
        match = re.search(r"\s+(\d+)/(\d+)$", name)
        if match is None:
            continue
        if int(match.group(1)) > keep_total or int(match.group(2)) != keep_total:
            empty_filtered_deck(deck["id"])
            remove_deck(deck["id"])

def create_split_study_decks(group_name: str, temporary: bool, search_queries: list, card_count: int, config: types.SimpleNamespace) -> tuple:
    total = len(search_queries)
    first_deck_id = None
    deck_names = []
    delete_extra_split_study_decks(group_name, temporary, total)
    for index, search_query in enumerate(search_queries, start=1):
        deck_name = split_study_deck_name(group_name, temporary, index, total)
        deck_id = create_or_update_filtered_deck(deck_name, search_query, card_count, config)
        deck_names.append(deck_name)
        if first_deck_id is None:
            first_deck_id = deck_id
    return (first_deck_id, deck_names)

def save_last_temp_study_deck_id(deck_id: int) -> None:
    saved_config = types.SimpleNamespace(**config_util.get_config(mw))
    saved_config.laststudydeckid = deck_id
    config_util.set_config(mw, saved_config)

def save_study_deck_note_watermark(note_id: int = None) -> None:
    saved_config = types.SimpleNamespace(**config_util.get_config(mw))
    saved_config.studydecklastnoteid = current_max_note_id() if note_id is None else note_id
    config_util.set_config(mw, saved_config)

def forget_last_temp_study_deck_id() -> None:
    saved_config = types.SimpleNamespace(**config_util.get_config(mw))
    if getattr(saved_config, "laststudydeckid", 0) == 0:
        return
    saved_config.laststudydeckid = 0
    config_util.set_config(mw, saved_config)

def remove_deck(deck_id: int) -> None:
    try:
        mw.col.decks.remove([deck_id])
    except TypeError:
        mw.col.decks.remove(deck_id)

def cleanup_temp_study_deck() -> None:
    try:
        saved_config = types.SimpleNamespace(**config_util.get_config(mw))
        if not getattr(saved_config, "makestudydecktemporary", True):
            return

        removed = False
        for deck in tracked_study_decks():
            if deck.get("name", "").startswith(TEMP_STUDY_DECK_NAME_PREFIX):
                empty_filtered_deck(deck["id"])
                remove_deck(deck["id"])
                removed = True

        deck_id = getattr(saved_config, "laststudydeckid", 0)
        if not deck_id:
            if removed:
                forget_last_temp_study_deck_id()
                if hasattr(mw, "reset"):
                    mw.reset()
            return

        deck = mw.col.decks.get(deck_id)
        if not deck or not deck.get("dyn") or not deck.get("name", "").startswith(TEMP_STUDY_DECK_NAME_PREFIX):
            forget_last_temp_study_deck_id()
            if removed and hasattr(mw, "reset"):
                mw.reset()
            return

        empty_filtered_deck(deck_id)
        remove_deck(deck_id)
        forget_last_temp_study_deck_id()
        if hasattr(mw, "reset"):
            mw.reset()
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to clean up Kanji Grid temp study deck", traceback.format_exc())

def reset_study_deck_runtime_state(*args, **kwargs) -> None:
    global startup_update_scheduled, study_deck_note_watch_timer, study_deck_note_watch_last_id
    startup_update_scheduled = False
    study_deck_note_watch_last_id = None
    if study_deck_note_watch_timer is not None:
        study_deck_note_watch_timer.stop()
        study_deck_note_watch_timer = None

def open_deck_for_study(deck_id: int) -> None:
    mw.col.decks.select(deck_id)
    if hasattr(mw, "onOverview"):
        mw.onOverview()
    elif hasattr(mw, "moveToState"):
        mw.moveToState("overview")

    def start_review() -> None:
        if hasattr(mw, "onStudy"):
            mw.onStudy()
        elif hasattr(mw, "moveToState"):
            mw.moveToState("review")

    QTimer.singleShot(250, start_review)

def create_study_deck_for_group(group_index: int, config: types.SimpleNamespace, units: dict, temporary: bool) -> tuple:
    if not hasattr(config, "fieldslist"):
        config.fieldslist = normalize_fields_list(shlex.split(getattr(config, "defaultfield", "")))
    else:
        config.fieldslist = normalize_fields_list(config.fieldslist)

    group_units = study_units_for_group(units, config, group_index)
    search_chars = search_chars_for_study_group(units, config, group_index, group_units)
    if len(search_chars) == 0:
        return (None, 0, "")

    search_query = group_search_query(config, group_index, search_chars)
    search_queries = None
    fallback_message = None
    if search_query is not None:
        try:
            card_count = len(mw.col.find_cards(search_query))
        except Exception as exception:
            fallback_message = str(exception)
            search_query = None

    if search_query is None and getattr(config, "splitbigdynamicqueries", False):
        search_queries = split_group_search_queries(config, group_index, search_chars)
        if search_queries is not None:
            try:
                card_count = len(find_cards_for_searches(search_queries))
            except Exception as exception:
                fallback_message = str(exception)
                search_queries = None

    if search_query is None and search_queries is None:
        notify_query_fallback(study_group_name(config, group_index), fallback_message)
        card_ids = unseen_card_ids_for_study_units(group_units)
        if len(card_ids) == 0:
            return (None, 0, "")
        search_query = cid_search(card_ids)
        card_count = len(card_ids)

    if card_count == 0:
        return (None, 0, "")

    group_name = study_group_name(config, group_index)
    deck_name = study_deck_name(group_name, temporary)
    if search_queries is not None:
        deck_id, deck_names = create_split_study_decks(group_name, temporary, search_queries, card_count, config)
        save_study_deck_configs(deck_names, config, group_name)
        deck_name = split_study_deck_name(group_name, temporary, 1, len(search_queries))
    else:
        deck_id = create_or_update_filtered_deck(deck_name, search_query, card_count, config)
        save_study_deck_configs([deck_name], config, group_name)
    save_study_deck_note_watermark()
    return (deck_id, study_deck_loaded_count(card_count, config), deck_name)

def on_create_study_deck_cmd(group_index_text: str, config: types.SimpleNamespace, units: dict) -> None:
    try:
        config_util.set_config(mw, config)
        group_index = int(group_index_text)
        deck_id, card_count, deck_name = create_study_deck_for_group(group_index, config, units, temporary=False)
        if deck_id is None:
            tooltip("No new cards found for this block.")
            return
        refresh_deck_layout()
        tooltip(f"Created {deck_name} with {card_count} new card(s).")
    except Exception as exception:  # noqa: BLE001
        logger.error_log("Failed to create Kanji Grid study deck", traceback.format_exc())
        showCritical("Failed to create study deck:\n" + str(exception))

def on_study_cmd(group_index_text: str, config: types.SimpleNamespace, units: dict) -> None:
    try:
        config_util.set_config(mw, config)
        group_index = int(group_index_text)
        temporary = getattr(config, "makestudydecktemporary", True)
        group_name = study_group_name(config, group_index)
        permanent_deck = filtered_permanent_study_deck_for_group_name(group_name)
        if permanent_deck is not None:
            deck_id, card_count, deck_name = create_study_deck_for_group(group_index, config, units, temporary=False)
            temporary = False
        else:
            deck_id, card_count, deck_name = create_study_deck_for_group(group_index, config, units, temporary=temporary)
        if deck_id is None:
            tooltip("No new cards found for this block.")
            return
        if temporary:
            save_last_temp_study_deck_id(deck_id)
        else:
            forget_last_temp_study_deck_id()
        open_deck_for_study(deck_id)
        tooltip(f"Loaded {card_count} cards into {deck_name}.")
    except Exception as exception:  # noqa: BLE001
        logger.error_log("Failed to start Kanji Grid temp study", traceback.format_exc())
        showCritical("Failed to start temp study:\n" + str(exception))

def update_existing_study_decks(config: types.SimpleNamespace = None) -> int:
    return update_matching_study_decks(config)

def update_matching_study_decks(config: types.SimpleNamespace = None, group_indexes: set = None, reset_ui: bool = True, recalculate_dynamic: bool = True, target_deck_ids: set = None) -> int:
    if config is None:
        config = types.SimpleNamespace(**config_util.get_config(mw))
    config = normalize_study_config(config)

    data.init_groups()
    units_by_deck = {}
    updated = 0
    fallback_count = 0
    for deck in tracked_study_decks():
        if target_deck_ids is not None and deck.get("id") not in target_deck_ids:
            continue
        group_name = study_deck_group_name(deck.get("name", ""))
        deck_config = config_for_study_deck(deck, config)
        group_index = study_group_index(deck_config, group_name)
        if group_index is None:
            continue
        if group_indexes is not None and group_index not in group_indexes:
            continue
        if not recalculate_dynamic and is_dynamic_study_deck(deck):
            continue

        units_key = repr(sorted(study_deck_config_snapshot(deck_config, group_name).items()))
        if units_key not in units_by_deck:
            units_by_deck[units_key] = generate_grid.kanjigrid(mw, deck_config)
        units = units_by_deck[units_key]

        group_units = study_units_for_group(units, deck_config, group_index)
        search_chars = search_chars_for_study_group(units, deck_config, group_index, group_units)
        search_query = group_search_query(deck_config, group_index, search_chars)
        search_queries = None
        card_count = 0
        if search_query is not None:
            try:
                card_count = len(mw.col.find_cards(search_query))
            except Exception:
                search_query = None

        if search_query is None and getattr(deck_config, "splitbigdynamicqueries", False):
            search_queries = split_group_search_queries(deck_config, group_index, search_chars)
            if search_queries is not None:
                try:
                    card_count = len(find_cards_for_searches(search_queries))
                except Exception:
                    search_queries = None

        if search_query is None and search_queries is None:
            fallback_count += 1
            logger.log(f"Kanji Grid query fallback used for {study_group_name(deck_config, group_index)}")
            card_ids = unseen_card_ids_for_study_units(group_units)
            search_query = cid_search(card_ids)
            card_count = len(card_ids)

        if search_queries is not None:
            split_index = split_study_deck_index(deck.get("name", ""))
            query_index = split_index[0] - 1 if split_index is not None else 0
            if query_index >= len(search_queries):
                continue
            deck["terms"] = [[search_queries[query_index], study_deck_limit_for_update(search_queries[query_index], card_count, deck_config, deck), 0]]
        else:
            deck["terms"] = [[search_query, study_deck_limit_for_update(search_query, card_count, deck_config, deck), 0]]
        deck["resched"] = True
        save_deck(deck)
        empty_filtered_deck(deck["id"])
        rebuild_filtered_deck(deck["id"])
        updated += 1

    if fallback_count > 0 and reset_ui:
        tooltip(f"Query fallback used for {fallback_count} Kanji Grid study deck(s).")
    if updated > 0 and reset_ui:
        refresh_deck_layout()
    return updated

def update_tracked_study_decks(config: types.SimpleNamespace = None) -> tuple:
    if config is None:
        config = types.SimpleNamespace(**config_util.get_config(mw))

    data.init_groups()
    clean_saved_study_deck_configs()
    dynamic_count = 0
    static_count = 0
    for deck in tracked_study_decks():
        deck_config = config_for_study_deck(deck, config)
        group_name = study_deck_group_name(deck.get("name", ""))
        group_index = study_group_index(deck_config, group_name)
        if group_index is None:
            continue
        if is_dynamic_study_deck(deck):
            deck_config.usequerystudydeck = True
            dynamic_count += update_matching_study_decks(deck_config, group_indexes={group_index}, reset_ui=False, recalculate_dynamic=True, target_deck_ids={deck["id"]})
        else:
            deck_config.usequerystudydeck = False
            static_count += update_matching_study_decks(deck_config, group_indexes={group_index}, reset_ui=False, recalculate_dynamic=False, target_deck_ids={deck["id"]})

    if dynamic_count + static_count > 0:
        refresh_deck_layout()
    return (dynamic_count, static_count)

def rebuild_dynamic_study_decks() -> int:
    clean_saved_study_deck_configs()
    dynamic_count = 0
    for deck in tracked_study_decks():
        if is_dynamic_study_deck(deck):
            deck_config = config_for_study_deck(deck, types.SimpleNamespace(**config_util.get_config(mw)))
            ensure_dynamic_study_deck_limit(deck, deck_config)
            rebuild_existing_filtered_deck(deck["id"])
            dynamic_count += 1
    return dynamic_count

def update_study_decks_after_collection_load(config: types.SimpleNamespace = None) -> tuple:
    global study_deck_note_watch_last_id
    if config is None:
        config = types.SimpleNamespace(**config_util.get_config(mw))
    config = normalize_study_config(config)

    dynamic_count = rebuild_dynamic_study_decks()
    static_count = 0

    max_id = current_max_note_id()
    old_id = getattr(config, "studydecklastnoteid", 0)
    if not old_id:
        study_deck_note_watch_last_id = max_id
        save_study_deck_note_watermark(max_id)
    elif max_id > old_id:
        note_ids = mw.col.db.list("select id from notes where id > ? and id <= ? order by id", old_id, max_id)
        static_count = append_new_notes_to_tracked_static_decks(note_ids, config)
        study_deck_note_watch_last_id = max_id
        save_study_deck_note_watermark(max_id)

    if dynamic_count + static_count > 0:
        refresh_deck_layout()
    return (dynamic_count, static_count)

def update_existing_study_decks_with_tooltip(config: types.SimpleNamespace = None) -> None:
    try:
        dynamic_count, static_count = update_tracked_study_decks(config)
        tooltip(f"Updated {dynamic_count} dynamic and {static_count} static Kanji Grid study deck(s).")
    except Exception as exception:  # noqa: BLE001
        logger.error_log("Failed to update Kanji Grid study decks", traceback.format_exc())
        showCritical("Failed to update study decks:\n" + str(exception))

def update_existing_study_decks_on_startup() -> None:
    global startup_update_scheduled
    startup_update_scheduled = False
    try:
        config = types.SimpleNamespace(**config_util.get_config(mw))
        if getattr(config, "updatestudydecksonstartup", True):
            dynamic_count, static_count = update_study_decks_after_collection_load(config)
            tooltip(f"Updated {dynamic_count} dynamic and {static_count} static Kanji Grid study deck(s) after collection load.")
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to update Kanji Grid study decks on startup", traceback.format_exc())

def schedule_existing_study_decks_startup_update(*args, **kwargs) -> None:
    global startup_update_scheduled
    if startup_update_scheduled:
        return
    startup_update_scheduled = True
    QTimer.singleShot(7000, update_existing_study_decks_on_startup)

def current_max_note_id() -> int:
    return int(mw.col.db.scalar("select coalesce(max(id), 0) from notes") or 0)

def touched_groups_for_notes(note_ids: list, config: types.SimpleNamespace):
    chars = set()
    for note_id in note_ids:
        try:
            note = mw.col.get_note(note_id)
        except Exception:  # noqa: BLE001
            continue
        chars.update(note_kanji_for_saved_fields(note, config))

    if len(chars) == 0:
        return None

    data.init_groups()
    if config.groupby <= 0:
        return {0}

    lookup = group_index_by_kanji(config)
    leftover_index = len(data.groupings[config.groupby - 1].groups)
    return {lookup.get(char, leftover_index) for char in chars}

def append_card_ids_to_filtered_deck(deck: dict, card_ids: set) -> None:
    if not card_ids:
        return

    existing_card_ids = set(mw.col.db.list("select id from cards where did = ?", deck["id"]))
    for term in deck.get("terms", []):
        if len(term) > 0:
            existing_card_ids.update(card_ids_from_cid_search(term[0]))

    merged_card_ids = sorted(existing_card_ids.union(card_ids))
    deck["terms"] = [[cid_search(merged_card_ids), len(merged_card_ids), 0]]
    deck["resched"] = True
    save_deck(deck)
    empty_filtered_deck(deck["id"])
    rebuild_filtered_deck(deck["id"])

def append_new_notes_to_study_decks(note_ids: list, config: types.SimpleNamespace) -> int:
    if len(note_ids) == 0:
        return 0

    data.init_groups()
    group_lookup = group_index_by_kanji(config) if config.groupby > 0 else {}
    leftover_index = len(data.groupings[config.groupby - 1].groups) if config.groupby > 0 else 0
    deck_card_ids = {}

    for note_id in note_ids:
        try:
            note = mw.col.get_note(note_id)
        except Exception:  # noqa: BLE001
            continue

        chars = note_kanji_for_saved_fields(note, config)
        if len(chars) == 0:
            continue

        group_indexes = {group_lookup.get(char, leftover_index) for char in chars} if config.groupby > 0 else {0}
        target_deck = None
        for group_index in sorted(group_indexes):
            target_deck = filtered_static_study_deck_for_group(config, group_index)
            if target_deck is not None:
                break
        if target_deck is None:
            continue

        card_ids = note_card_ids_matching_grid_scope(note_id, config)
        if not card_ids:
            continue
        deck_card_ids.setdefault(target_deck["id"], set()).update(card_ids)

    updated = 0
    for deck_id, card_ids in deck_card_ids.items():
        deck = mw.col.decks.get(deck_id)
        if deck and deck.get("dyn") and is_static_study_deck(deck):
            append_card_ids_to_filtered_deck(deck, card_ids)
            updated += 1

    if updated > 0:
        refresh_deck_layout()
    return updated

def append_new_notes_to_tracked_static_decks(note_ids: list, fallback_config: types.SimpleNamespace = None) -> int:
    clean_saved_study_deck_configs()
    updated = 0
    seen_configs = set()
    for deck in tracked_study_decks():
        if not is_static_study_deck(deck):
            continue
        deck_config = config_for_study_deck(deck, fallback_config)
        config_key = repr(sorted(study_deck_config_snapshot(deck_config, study_deck_group_name(deck.get("name", ""))).items()))
        if config_key in seen_configs:
            continue
        seen_configs.add(config_key)
        updated += append_new_notes_to_study_decks(note_ids, deck_config)
    return updated

def poll_added_notes_for_study_decks() -> None:
    global study_deck_note_watch_last_id
    try:
        if mw is None or mw.col is None:
            return

        max_id = current_max_note_id()
        if study_deck_note_watch_last_id is None:
            saved_config = types.SimpleNamespace(**config_util.get_config(mw))
            study_deck_note_watch_last_id = getattr(saved_config, "studydecklastnoteid", 0) or max_id

        if max_id <= study_deck_note_watch_last_id:
            return

        old_id = study_deck_note_watch_last_id
        study_deck_note_watch_last_id = max_id
        save_study_deck_note_watermark(max_id)

        config = types.SimpleNamespace(**config_util.get_config(mw))
        if not getattr(config, "rebuildstudydecksonnoteadd", False):
            return

        note_ids = mw.col.db.list("select id from notes where id > ? and id <= ? order by id", old_id, max_id)
        config = normalize_study_config(config)

        dynamic_count = rebuild_dynamic_study_decks()
        static_count = append_new_notes_to_tracked_static_decks(note_ids, config)
        if dynamic_count + static_count > 0:
            refresh_deck_layout()
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to poll added notes for Kanji Grid study decks", traceback.format_exc())

def start_study_deck_note_watcher(*args, **kwargs) -> None:
    global study_deck_note_watch_timer, study_deck_note_watch_last_id
    if mw is None or mw.col is None:
        return
    saved_config = types.SimpleNamespace(**config_util.get_config(mw))
    saved_last_id = getattr(saved_config, "studydecklastnoteid", 0)
    study_deck_note_watch_last_id = saved_last_id or current_max_note_id()
    if not saved_last_id:
        save_study_deck_note_watermark(study_deck_note_watch_last_id)
    if study_deck_note_watch_timer is not None:
        return

    study_deck_note_watch_timer = QTimer(mw)
    study_deck_note_watch_timer.setInterval(4000)
    study_deck_note_watch_timer.timeout.connect(poll_added_notes_for_study_decks)
    study_deck_note_watch_timer.start()

def group_index_by_kanji(config: types.SimpleNamespace) -> dict:
    if config.groupby <= 0:
        return {}
    grouping = data.groupings[config.groupby - 1]
    result = {}
    for index, group in enumerate(grouping.groups):
        for char in group.characters:
            result[char] = index
    return result

def note_kanji_for_saved_fields(note, config: types.SimpleNamespace) -> set:
    fields = shlex.split(getattr(config, "defaultfield", "").lower())
    if len(fields) == 0:
        return set()

    chars = set()
    for field_name in note.keys():
        if field_name.lower() not in fields:
            continue
        for char in note[field_name]:
            if util.is_kanji(char):
                chars.add(char)
    return chars

def update_study_decks_for_added_note(note) -> None:
    try:
        config = types.SimpleNamespace(**config_util.get_config(mw))
        if not getattr(config, "rebuildstudydecksonnoteadd", False):
            return
        QTimer.singleShot(3000, poll_added_notes_for_study_decks)
    except Exception:  # noqa: BLE001
        logger.error_log("Failed to update Kanji Grid study decks for added note", traceback.format_exc())

def update_study_decks_for_added_note_hook(*args, **kwargs) -> None:
    note = kwargs.get("note")
    if note is None:
        for arg in args:
            if hasattr(arg, "keys") and hasattr(arg, "__getitem__"):
                note = arg
                break
    if note is not None:
        update_study_decks_for_added_note(note)

def on_search_cmd(char: str, wv: AnkiWebView, config: types.SimpleNamespace) -> None:
    open_search_link(wv, config, char)

def on_find_cmd(wv: AnkiWebView) -> None:
    char = QApplication.clipboard().text().strip()

    # limit searches to kanji to prevent js injection
    if not util.is_kanji(char):
        # truncate in case there's random garbage in the clipboard
        len_limit = 20
        tooltip_char = char if len(char) <= len_limit else char[:len_limit] + "..."
        tooltip(f"\"{tooltip_char}\" is not valid kanji.")
        return

    def find_text_callback(found: bool) -> None:
        if not found:
          tooltip(f"\"{char}\" not found in grid.")

    # qt's findText impl is bugged and inconvenient, so we use our own
    wv.evalWithCallback(f"findChar('{char}');", find_text_callback)

def add_webview_context_menu_items(wv: AnkiWebView, expected_wv: AnkiWebView, menu, config: types.SimpleNamespace, deckname: str, char: str) -> None:
    # hook is active while kanjigrid is open, and right clicking on the main window (deck list) will also trigger this, so check wv
    if wv is not expected_wv:
      return
    if char != "":
        menu.clear()
        copy_action = menu.addAction(f"Copy {char} to clipboard")
        qconnect(copy_action.triggered, lambda: on_copy_cmd(char))
        browse_action = menu.addAction(f"Browse deck for {char}")
        qconnect(browse_action.triggered, lambda: on_browse_cmd(char, config, deckname))
        search_action = menu.addAction(f"Search online for {char}")
        qconnect(search_action.triggered, lambda: on_search_cmd(char, wv, config))
    else:
        find_action = menu.addAction("Find copied kanji")
        qconnect(find_action.triggered, lambda: on_find_cmd(wv))
