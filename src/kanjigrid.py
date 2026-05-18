import shlex
import os
import types

from aqt import gui_hooks, main, mw
from aqt.qt import (
    QAction,
    QCheckBox,
    QComboBox,
    QDateTime,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    Qt,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.webview import AnkiWebView

from . import config_util, data, generate_grid, save, util, webview_util


class KanjiGrid:
    def __init__(self, mw: main.AnkiQt) -> None:
        if mw:
            self.menuAction = QAction("Generate Kanji Grid", mw, triggered=self.setup)
            mw.form.menuTools.addSeparator()
            mw.form.menuTools.addAction(self.menuAction)

    def link_handler(self, link: str, config: types.SimpleNamespace, deckname: str) -> None:
        link_prefix = link[:2]
        link_suffix = link[2:]
        if link_prefix == "h:":
            self.hovered = link_suffix
        elif link_prefix == "l:":
            if link_suffix == self.hovered:
                # clear when outside grid
                self.hovered = ""
        else:
            webview_util.on_browse_cmd(link, config, deckname)

    def displaygrid(self, config: types.SimpleNamespace, deckname: str, units: dict) -> None:
        generated_html = generate_grid.generate(mw, config, units)
        self.win = QDialog(mw, Qt.WindowType.Window)
        current_win = self.win
        self.wv = webview_util.init_webview()
        current_wv = self.wv

        def on_window_close(current_wv: AnkiWebView) -> None:
            current_wv.cleanup()
            gui_hooks.webview_will_show_context_menu.remove(webview_util.add_webview_context_menu_items)
        qconnect(current_win.finished, lambda _: on_window_close(current_wv))
        mw.garbage_collect_on_dialog_finish(current_win)

        self.hovered = ""
        current_wv.set_bridge_command(lambda link: self.link_handler(link, config, deckname), None)
        # add webview context menu hook and defer cleanup (in on_window_close)
        gui_hooks.webview_will_show_context_menu.append(lambda wv, menu: webview_util.add_webview_context_menu_items(wv, current_wv, menu, config, deckname, self.hovered))

        vl = QVBoxLayout()
        vl.setContentsMargins(0, 0, 0, 0)
        vl.addWidget(current_wv)
        current_wv.stdHtml(generated_html)
        hl = QHBoxLayout()
        vl.addLayout(hl)
        save_html = QPushButton("Save HTML", clicked=lambda: save.savehtml(mw, current_win, config, deckname))
        hl.addWidget(save_html)
        same_image = QPushButton("Save Image", clicked=lambda: save.savepng(current_wv, current_win, config, deckname))
        hl.addWidget(same_image)
        save_pdf = QPushButton("Save PDF", clicked=lambda: save.savepdf(mw, current_wv, current_win, deckname))
        hl.addWidget(save_pdf)
        save_json = QPushButton("Save JSON", clicked=lambda: save.savejson(mw, current_win, config, deckname, units))
        hl.addWidget(save_json)
        save_txt = QPushButton("Save TXT", clicked=lambda: save.savetxt(mw, current_win, config, deckname, units))
        hl.addWidget(save_txt)
        bb = QPushButton("Close", clicked=current_win.reject)
        hl.addWidget(bb)
        current_win.setLayout(vl)
        current_win.resize(1000, 800)

    def makegrid(self, config: types.SimpleNamespace) -> None:
        units = generate_grid.kanjigrid(mw, config)
        if units is not None:
            self.displaygrid(config, util.get_deck_name(mw, config), units)

    def setup(self) -> None:
        config = types.SimpleNamespace(**config_util.get_config(mw))
        config.did = mw.col.conf["curDeck"]
        def change_did(deckname: str) -> None:
            if deckname == "*":
                config.did = "*"
                return
            selected_deck_info = mw.col.decks.by_name(deckname)
            if selected_deck_info:
                config.did = selected_deck_info["id"]
            else:
                config.did = "*"
                deckcb.setCurrentText("*")

        data.init_groups()

        setup_win = QDialog(mw)
        vertical_layout = QVBoxLayout()

        deck_horizontal_layout = QHBoxLayout()
        deckcb = QComboBox()
        deckcb.addItem("*") # * = all decks
        deckcb.addItems(sorted(mw.col.decks.all_names()))
        deckcb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        deck_horizontal_layout.addWidget(QLabel("Deck: "))

        default_deck_name = config.defaultdeck
        if default_deck_name == "":
            default_deck_name = mw.col.decks.get(config.did)["name"]
        deckcb.setCurrentText(default_deck_name)
        change_did(default_deck_name)
        deckcb.currentTextChanged.connect(change_did)

        deck_horizontal_layout.addWidget(deckcb)
        vertical_layout.addLayout(deck_horizontal_layout)

        tabs_frame = QTabWidget()
        vertical_layout.addWidget(tabs_frame)

        #General Tab
        general_tab = QWidget()
        general_tab_scroll_area = QScrollArea()
        general_tab_scroll_area.setWidgetResizable(True)
        general_tab_vertical_layout = QVBoxLayout()
        general_tab_vertical_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        field_horizontal_layout = QHBoxLayout()
        general_tab_vertical_layout.addWidget(QLabel("Fields: "))
        field = QComboBox()
        field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        def update_fields_dropdown(deckname: str) -> None:
            if deckname != "*":
                deckname = mw.col.decks.get(config.did)["name"]
            new_text = set()
            field_names = []
            for item in mw.col.models.all_names_and_ids():
                model_id_name = str(item).replace("id: ", "").replace("name: ", "").replace("\"", "").split("\n")
                # Anki backend will return incorrectly escaped strings that need to be stripped of `\`. However, `"`, `*`, and `_` should not be stripped
                model_name = model_id_name[1].replace("\\", "").replace("*", "\\*").replace("_", "\\_").replace("\"", "\\\"")
                if len(mw.col.find_cards("\"note:" + model_name + "\" " + "\"deck:" + deckname + "\"")) > 0:
                    model_id = model_id_name[0]
                    model_fields = mw.col.models.get(model_id)["flds"]
                    for field_dict in model_fields:
                        field_dict_name = field_dict["name"]
                        if len(field_dict_name.split()) > 1:
                            field_dict_name = "\"" + field_dict_name + "\""
                        field_names.append(field_dict_name)

                    if len(model_fields) > 0:
                        first_field_name = model_fields[0]["name"]
                        if len(first_field_name.split()) > 1:
                            first_field_name = "\"" + first_field_name + "\""
                        new_text.add(first_field_name)
            field.clear()
            field.addItems(field_names)
            if config.defaultfield != "":
                field.setCurrentText(config.defaultfield)
            else:
                field.setCurrentText(" ".join(new_text))
        field.setEditable(True)
        deckcb.currentTextChanged.connect(update_fields_dropdown)
        update_fields_dropdown(config.did)
        field_horizontal_layout.addWidget(field)
        general_tab_vertical_layout.addLayout(field_horizontal_layout)

        text_source_checkbox = QCheckBox("Use external export")
        text_source_checkbox.setChecked(False)
        general_tab_vertical_layout.addWidget(text_source_checkbox)

        text_source_mix = QCheckBox("Mix with deck (Anki takes priority)")
        text_source_mix.setChecked(True)
        text_source_mix.setEnabled(False)
        general_tab_vertical_layout.addWidget(text_source_mix)

        text_source_kind = QComboBox()
        text_source_kind.addItem("Plain TXT export", "txt")
        text_source_kind.addItem("Jiten backup export", "jiten")
        text_source_kind.setEnabled(False)
        general_tab_vertical_layout.addWidget(text_source_kind)

        text_source_horizontal_layout = QHBoxLayout()
        text_source_path = QLineEdit()
        text_source_path.setPlaceholderText("One word per line")
        text_source_path.setEnabled(False)
        text_source_browse = QPushButton("Browse")
        text_source_browse.setEnabled(False)

        def browse_text_source() -> None:
            if text_source_kind.currentData() == "jiten":
                file_name = QFileDialog.getOpenFileName(setup_win, "Select Jiten Backup Export", "", "JSON Files (*.json);;All Files (*)")[0]
            else:
                file_name = QFileDialog.getOpenFileName(setup_win, "Select TXT Word List", "", "Text Files (*.txt);;All Files (*)")[0]
            if file_name != "":
                text_source_path.setText(file_name)

        def update_text_source_kind() -> None:
            if text_source_kind.currentData() == "jiten":
                text_source_path.setPlaceholderText("Jiten vocabulary backup JSON")
            else:
                text_source_path.setPlaceholderText("One word per line")

        text_source_checkbox.toggled.connect(text_source_path.setEnabled)
        text_source_checkbox.toggled.connect(text_source_browse.setEnabled)
        text_source_checkbox.toggled.connect(text_source_mix.setEnabled)
        text_source_checkbox.toggled.connect(text_source_kind.setEnabled)
        text_source_kind.currentTextChanged.connect(lambda _: update_text_source_kind())
        text_source_browse.clicked.connect(lambda _: browse_text_source())
        text_source_horizontal_layout.addWidget(text_source_path)
        text_source_horizontal_layout.addWidget(text_source_browse)
        general_tab_vertical_layout.addLayout(text_source_horizontal_layout)

        jmdict_horizontal_layout = QHBoxLayout()
        jmdict_path = QLineEdit()
        jmdict_path.setPlaceholderText("JMdict Yomitan ZIP")
        jmdict_path.setEnabled(False)
        jmdict_browse = QPushButton("Browse JMdict")
        jmdict_browse.setEnabled(False)

        def update_jmdict_controls() -> None:
            enabled = text_source_checkbox.isChecked() and text_source_kind.currentData() == "jiten"
            jmdict_path.setEnabled(enabled)
            jmdict_browse.setEnabled(enabled)

        def browse_jmdict() -> None:
            file_name = QFileDialog.getOpenFileName(setup_win, "Select JMdict Yomitan ZIP", "", "ZIP Files (*.zip);;All Files (*)")[0]
            if file_name != "":
                jmdict_path.setText(file_name)

        text_source_checkbox.toggled.connect(lambda _: update_jmdict_controls())
        text_source_kind.currentTextChanged.connect(lambda _: update_jmdict_controls())
        jmdict_browse.clicked.connect(lambda _: browse_jmdict())
        jmdict_horizontal_layout.addWidget(jmdict_path)
        jmdict_horizontal_layout.addWidget(jmdict_browse)
        general_tab_vertical_layout.addLayout(jmdict_horizontal_layout)
        update_text_source_kind()
        update_jmdict_controls()

        gsm_source_checkbox = QCheckBox("Use GSM encounters CSV")
        gsm_source_checkbox.setChecked(False)
        general_tab_vertical_layout.addWidget(gsm_source_checkbox)

        gsm_source_horizontal_layout = QHBoxLayout()
        gsm_source_path = QLineEdit()
        gsm_source_path.setPlaceholderText("GSM CSV export")
        gsm_source_path.setEnabled(False)
        gsm_source_browse = QPushButton("Browse GSM")
        gsm_source_browse.setEnabled(False)

        def browse_gsm_source() -> None:
            file_name = QFileDialog.getOpenFileName(setup_win, "Select GSM CSV Export", "", "CSV Files (*.csv);;All Files (*)")[0]
            if file_name != "":
                gsm_source_path.setText(file_name)

        gsm_source_checkbox.toggled.connect(gsm_source_path.setEnabled)
        gsm_source_checkbox.toggled.connect(gsm_source_browse.setEnabled)
        gsm_source_browse.clicked.connect(lambda _: browse_gsm_source())
        gsm_source_horizontal_layout.addWidget(gsm_source_path)
        gsm_source_horizontal_layout.addWidget(gsm_source_browse)
        general_tab_vertical_layout.addLayout(gsm_source_horizontal_layout)

        groupby = QComboBox()
        groupby.addItems([
            "None",
            *(("[" + x.lang + "] " + util.truncate_text(x.name, 60)) for x in data.groupings),
        ])
        groupby.setCurrentIndex(config.groupby)
        general_tab_vertical_layout.addWidget(QLabel("Group by:"))
        general_tab_vertical_layout.addWidget(groupby)

        sortby = QComboBox()
        sortby.addItems([
            *(x.pretty_value().title() for x in util.SortOrder),
        ])
        sortby.setCurrentIndex(config.sortby)
        general_tab_vertical_layout.addWidget(QLabel("Sort by:"))
        general_tab_vertical_layout.addWidget(sortby)

        pagelang = QComboBox()
        pagelang.addItems(["ja", "zh","zh-Hans", "zh-Hant", "ko", "vi"])
        def update_pagelang_dropdown() -> None:
            index = groupby.currentIndex() - 1
            if index > 0:
                pagelang.setCurrentText(data.groupings[index].lang)
        groupby.currentTextChanged.connect(update_pagelang_dropdown)
        pagelang.setCurrentText(config.lang)
        general_tab_vertical_layout.addWidget(QLabel("Language:"))
        general_tab_vertical_layout.addWidget(pagelang)

        shnew = QCheckBox("Show units not yet seen")
        shnew.setChecked(config.unseen)
        general_tab_vertical_layout.addWidget(shnew)

        general_tab.setLayout(general_tab_vertical_layout)
        general_tab_scroll_area.setWidget(general_tab)
        tabs_frame.addTab(general_tab_scroll_area, "General")

        #Advanced Tab
        advanced_tab = QWidget()
        advanced_tab_scroll_area = QScrollArea()
        advanced_tab_scroll_area.setWidgetResizable(True)
        advanced_tab_vertical_layout = QVBoxLayout()
        advanced_tab_vertical_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        strong_interval = QSpinBox()
        strong_interval.setRange(1, 65536)
        strong_interval.setValue(config.interval)
        advanced_tab_vertical_layout.addWidget(QLabel("Card interval considered strong:"))
        advanced_tab_vertical_layout.addWidget(strong_interval)

        search_filter = QLineEdit()
        search_filter.setText(config.searchfilter)
        search_filter.setPlaceholderText("e.g. \"is:new\" or \"tag:mining_deck\"")
        advanced_tab_vertical_layout.addWidget(QLabel("Additional Search Filters:"))
        advanced_tab_vertical_layout.addWidget(search_filter)

        time_travel_datetime = QDateTimeEdit()
        time_travel_default_time = QDateTime.currentDateTime()
        time_travel_datetime.setDateTime(time_travel_default_time)
        time_travel_datetime.setCalendarPopup(True)
        advanced_tab_vertical_layout.addWidget(QLabel("Time Travel:"))
        advanced_tab_vertical_layout.addWidget(time_travel_datetime)
        time_travel_note = QLabel("Generated grid might not match actual past grid exactly")
        time_travel_note.setStyleSheet("color: gray")
        advanced_tab_vertical_layout.addWidget(time_travel_note)

        advanced_tab.setLayout(advanced_tab_vertical_layout)
        advanced_tab_scroll_area.setWidget(advanced_tab)
        tabs_frame.addTab(advanced_tab_scroll_area, "Advanced")

        #Data Tab
        data_tab = QWidget()
        data_tab_scroll_area = QScrollArea()
        data_tab_scroll_area.setWidgetResizable(True)
        data_tab_vertical_layout = QVBoxLayout()
        data_tab_vertical_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        def set_config_attributes(config: types.SimpleNamespace) -> types.SimpleNamespace:
            config.fieldslist = shlex.split(field.currentText().lower())
            config.usetextsource = text_source_checkbox.isChecked()
            config.mixtextsource = text_source_mix.isChecked()
            config.textsourcekind = text_source_kind.currentData()
            config.textsourcepath = text_source_path.text()
            config.jmdictpath = jmdict_path.text()
            config.usegsmsource = gsm_source_checkbox.isChecked()
            config.gsmsourcepath = gsm_source_path.text()
            if save_defaultdeck.isChecked():
                config.defaultdeck = deckcb.currentText()
            if save_defaultfield.isChecked():
                config.defaultfield = field.currentText()
            config.searchfilter = search_filter.text()
            config.interval = strong_interval.value()
            config.groupby = groupby.currentIndex()
            config.sortby = sortby.currentIndex()
            config.lang = pagelang.currentText()
            config.unseen = shnew.isChecked()
            config.timetravel_enabled = time_travel_default_time.toMSecsSinceEpoch() != time_travel_datetime.dateTime().toMSecsSinceEpoch()
            config.timetravel_time = time_travel_datetime.dateTime().toMSecsSinceEpoch()
            return config

        data_tab_vertical_layout.addWidget(QLabel("Save grid without rendering:"))
        save_grid_buttons_horizontal_layout = QHBoxLayout()
        data_tab_vertical_layout.addLayout(save_grid_buttons_horizontal_layout)

        def save_html_grid(config: types.SimpleNamespace) -> None:
            new_config = set_config_attributes(config)
            save.savehtml(mw, mw, new_config, util.get_deck_name(mw, new_config))

        save_html_button = QPushButton("Save HTML", clicked = lambda _: save_html_grid(config))
        save_grid_buttons_horizontal_layout.addWidget(save_html_button)

        def save_json_grid(config: types.SimpleNamespace) -> None:
            new_config = set_config_attributes(config)
            units = generate_grid.kanjigrid(mw, new_config)
            save.savejson(mw, mw, new_config, util.get_deck_name(mw, new_config), units)

        save_json_button = QPushButton("Save JSON", clicked = lambda _: save_json_grid(config))
        save_grid_buttons_horizontal_layout.addWidget(save_json_button)

        def save_txt_grid(config: types.SimpleNamespace) -> None:
            new_config = set_config_attributes(config)
            units = generate_grid.kanjigrid(mw, new_config)
            save.savetxt(mw, mw, new_config, util.get_deck_name(mw, new_config), units)

        save_txt_button = QPushButton("Save TXT", clicked = lambda _: save_txt_grid(config))
        save_grid_buttons_horizontal_layout.addWidget(save_txt_button)

        data_tab_vertical_layout.addWidget(QLabel("Timelapse:"))
        timelapse_dates_horizontal_layout = QHBoxLayout()
        data_tab_vertical_layout.addLayout(timelapse_dates_horizontal_layout)
        timelapse_default_time = QDateTime.currentDateTime()
        timelapse_start_time = QDateTimeEdit()
        timelapse_start_time.setDateTime(timelapse_default_time)
        timelapse_start_time.setCalendarPopup(True)
        timelapse_start_time.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        timelapse_dates_horizontal_layout.addWidget(timelapse_start_time)
        timelapse_end_time = QDateTimeEdit()
        timelapse_end_time.setDateTime(timelapse_default_time)
        timelapse_end_time.setCalendarPopup(True)
        timelapse_end_time.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        timelapse_dates_inbetween_label = QLabel("to")
        timelapse_dates_inbetween_label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        timelapse_dates_horizontal_layout.addWidget(timelapse_dates_inbetween_label)
        timelapse_dates_horizontal_layout.addWidget(timelapse_end_time)
        timelapse_step_length = QLineEdit()
        timelapse_step_length.setText("10")
        timelapse_steps_horizontal_layout = QHBoxLayout()
        timelapse_steps_horizontal_layout.addWidget(QLabel("Step size (days):"))
        timelapse_steps_horizontal_layout.addWidget(timelapse_step_length)
        data_tab_vertical_layout.addLayout(timelapse_steps_horizontal_layout)

        def generate_timelapse(config: types.SimpleNamespace) -> None:
            step_size = int(float(timelapse_step_length.text()) * 86400000)
            save.savetimelapsejson(mw, mw, set_config_attributes(config), util.get_deck_name(mw, config), timelapse_start_time.dateTime().toMSecsSinceEpoch(), timelapse_end_time.dateTime().toMSecsSinceEpoch(), step_size)

        generate_timelapse_button = QPushButton("Generate Timelapse Data", clicked = lambda _: generate_timelapse(config))
        data_tab_vertical_layout.addWidget(generate_timelapse_button)
        timelapse_note = QLabel("Timelapse data requires external tools to process")
        timelapse_note.setStyleSheet("color: gray")
        data_tab_vertical_layout.addWidget(timelapse_note)

        data_tab_vertical_layout.addWidget(QLabel("Manage settings:"))

        save_options_horizontal_layout = QHBoxLayout()
        data_tab_vertical_layout.addLayout(save_options_horizontal_layout)

        save_defaultdeck = QCheckBox("Save deck")
        save_defaultdeck.setChecked(False)
        save_options_horizontal_layout.addWidget(save_defaultdeck)

        save_defaultfield = QCheckBox("Save fields")
        save_defaultfield.setChecked(False)
        save_options_horizontal_layout.addWidget(save_defaultfield)

        save_reset_buttons_horizontal_layout = QHBoxLayout()
        data_tab_vertical_layout.addLayout(save_reset_buttons_horizontal_layout)

        def save_settings(config: types.SimpleNamespace) -> None:
            config_util.set_config(mw, set_config_attributes(config))

        save_settings_button = QPushButton("Save Settings", clicked = lambda _: save_settings(config))
        save_reset_buttons_horizontal_layout.addWidget(save_settings_button)

        def reset_settings(setup_win: QDialog) -> None:
            reply = QMessageBox.question(setup_win, "Reset Settings", "Confirm reset settings")
            if reply == QMessageBox.StandardButton.Yes:
                config_util.reset_config(mw)
                setup_win.reject()

        reset_settings_button = QPushButton("Reset Settings", clicked = lambda _: reset_settings(setup_win))
        save_reset_buttons_horizontal_layout.addWidget(reset_settings_button)

        data_tab.setLayout(data_tab_vertical_layout)
        data_tab_scroll_area.setWidget(data_tab)
        tabs_frame.addTab(data_tab_scroll_area, "Data")

        #Bottom Buttons
        bottom_buttons_horizontal_layout = QHBoxLayout()
        vertical_layout.addLayout(bottom_buttons_horizontal_layout)

        def accept_if_valid() -> None:
            if text_source_checkbox.isChecked() and text_source_path.text().strip() == "":
                QMessageBox.warning(setup_win, "External Export", "Select an export file first.")
                return
            if text_source_checkbox.isChecked() and not os.path.isfile(text_source_path.text()):
                QMessageBox.warning(setup_win, "External Export", "The selected export file does not exist.")
                return
            if text_source_checkbox.isChecked() and text_source_kind.currentData() == "jiten" and not os.path.isfile(jmdict_path.text()):
                QMessageBox.warning(setup_win, "Jiten Backup Export", "Select the JMdict Yomitan ZIP first.")
                return
            if gsm_source_checkbox.isChecked() and gsm_source_path.text().strip() == "":
                QMessageBox.warning(setup_win, "GSM CSV Export", "Select a GSM CSV file first.")
                return
            if gsm_source_checkbox.isChecked() and not os.path.isfile(gsm_source_path.text()):
                QMessageBox.warning(setup_win, "GSM CSV Export", "The selected GSM CSV file does not exist.")
                return
            setup_win.accept()

        generate_button = QPushButton("Generate", clicked = accept_if_valid)
        bottom_buttons_horizontal_layout.addWidget(generate_button)
        close_button = QPushButton("Close", clicked = setup_win.reject)
        bottom_buttons_horizontal_layout.addWidget(close_button)

        setup_win.setLayout(vertical_layout)
        setup_win.resize(500, 400)
        if setup_win.exec():
            mw.progress.start(immediate=True)
            config = set_config_attributes(config)
            self.makegrid(config)
            mw.progress.finish()
            self.win.show()
